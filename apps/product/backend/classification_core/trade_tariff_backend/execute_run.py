"""Runs every gold query for one already-created run, entirely through
TradeTariffBackendClient. Factored out (per the AI-1073 design's "Approach B") so both
a local CLI invocation and the FastAPI ingress route call this same, tested
function rather than duplicating the loop.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from openai import AsyncOpenAI

from classification_core.provider_guard import openai_allowed

from .qa_loop import run_qa_session_via_trade_tariff_backend


def _matches_gold(candidate_code, expected_code, expected_code_digits) -> bool:
    """True when a search result is the gold commodity, compared at the gold
    query's OWN granularity.

    A gold query's expected_code is stored at whatever depth the source ruling
    actually published — 6, 8 or 10 digits, never right-padded (see
    trade-tariff-backend's EvaluationGoldQuery, and AI-1156 which added
    expected_code_digits precisely so consumers stop assuming a 10-digit leaf).
    Search results, by contrast, always carry a full 10-digit
    goods_nomenclature_item_id. Comparing those with plain `==` scored every
    6- or 8-digit gold query as wrong even when search returned exactly the
    right commodity, and did so with error=None, so it read as a legitimate
    (but false) accuracy measurement.

    expected_code_digits is the backend's own explicit statement of that
    granularity, and is the value we compare on. If it is ever missing we fall
    back to the expected code's actual length — server-side it is defined as
    exactly that (EvaluationGoldQuery#expected_code_digits returns
    expected_code&.length), so the fallback is the same answer, and it keeps a
    missing field from turning every candidate into a silent miss.
    """
    if not candidate_code or not expected_code:
        return False
    digits = expected_code_digits or len(expected_code)
    return candidate_code[:digits] == expected_code


async def execute_run(run_id: str, client) -> dict:
    succeeded = 0
    failed = 0
    gold_queries: list = []
    # This outer try/except covers EVERYTHING from the initial get_run/
    # update_run(status="running") onward — not just the per-gold-query
    # post_result call. It used to start only after those two calls, which
    # meant a failure there (backend unreachable, backend down, a validation
    # error) raised straight out of execute_run, uncaught by main.py's
    # background task wrapper too, and the run was left stranded at whatever
    # status it already had (e.g. "queued") with zero error recorded anywhere
    # — the only trace was an "exception was never retrieved" line in the
    # eval app's own server log. Wrapping these calls too means execute_run
    # always attempts the final update_run below, so a run that never even
    # reached "running" still ends up "failed" with an error_summary instead
    # of stranded silently.
    outer_exc: Exception | None = None
    run: dict = {}
    try:
        run = await client.get_run(run_id)
        await client.update_run(run_id, status="running")

        gold_queries = await client.get_gold_queries()

        # Built once for the whole run, the same way classification_core.qa_loop's
        # existing run_qa_session builds it. openai_allowed() is this repo's spend
        # gate (CLASSIFICATION_ALLOW_PROVIDER_CALLS + OPENAI_API_KEY): with it off,
        # sim_client stays None and any gold query that reaches a clarifying
        # question fails fast and explicitly via simulator_failed, rather than
        # silently spending credits.
        api_key = os.environ.get("OPENAI_API_KEY")
        sim_client = AsyncOpenAI(api_key=api_key) if (openai_allowed() and api_key) else None

        for gold in gold_queries:
            # Read the identity fields ONCE, before the try, via .get(). The except
            # handler below builds a failure result from these same fields — if it
            # read them off the gold dict itself, a malformed row would make the
            # handler raise too, escaping execute_run entirely and stranding the run
            # at status="running" with update_run never called again.
            source_type = gold.get("source_type")
            source_id = gold.get("source_id")
            persona = gold.get("persona")
            expected_code = gold.get("expected_code")
            expected_code_digits = gold.get("expected_code_digits")

            try:
                ruling = await client.get_atar_ruling(source_id)
                oracle_text = ruling.get("description") or ruling.get("justification") or ""

                session_result = await run_qa_session_via_trade_tariff_backend(
                    # No oracle text means there is nothing for the simulator to
                    # answer from — matches the "and oracle_text" gate qa_loop.py
                    # uses when it builds its own sim_client. Without this, a gold
                    # query missing both description and justification still got a
                    # simulated (ungrounded) answer that scored as a legitimate
                    # result instead of a failure.
                    client=client, sim_client=sim_client if oracle_text else None, query=gold["query"], oracle_text=oracle_text,
                    run_time_overrides=run["effective_configuration"], max_rounds=run["effective_configuration"].get("max_rounds", 5),
                )
                if session_result.get("simulator_failed"):
                    # Never score a fabricated answer — route it through the same
                    # failure-recording path as any other exception below.
                    raise RuntimeError("simulator exhausted its retries without producing an answer")

                final_candidates = session_result["final_candidates"]
                # Indexed directly, not via .get(): a search response missing these
                # keys is a genuine problem, and inside this try it is recorded as a
                # failed result with an error rather than quietly scored as a miss.
                candidate_codes = [c["attributes"]["goods_nomenclature_item_id"] for c in final_candidates]
                final_code = candidate_codes[0] if candidate_codes else None
                top5_codes = candidate_codes[:5]
                final_rank = next(
                    (
                        index + 1
                        for index, code in enumerate(candidate_codes)
                        if _matches_gold(code, expected_code, expected_code_digits)
                    ),
                    None,
                )

                await client.post_result({
                    "run_id": run_id, "source_type": source_type, "source_id": source_id,
                    "persona": persona, "expected_code": expected_code, "final_code": final_code,
                    "final_rank": final_rank,
                    "gold_in_top1": _matches_gold(final_code, expected_code, expected_code_digits),
                    "gold_in_top5": any(_matches_gold(c, expected_code, expected_code_digits) for c in top5_codes),
                    "error": None,
                    # .get() with a default, not direct indexing: every real
                    # run_qa_session_via_trade_tariff_backend call includes these
                    # (see qa_loop.py's usage_totals()), but a genuinely absent key
                    # here means "no usage data available" the same way an absent
                    # meta.usage on a /searches round does -- zero, not a crash.
                    "cost_usd": session_result.get("cost_usd", 0.0),
                    "latency_seconds": session_result.get("latency_seconds", 0.0),
                    "provider_calls": session_result.get("provider_calls", 0),
                })
                succeeded += 1
            except Exception as exc:  # noqa: BLE001 - one bad gold query must not abort the run
                try:
                    await client.post_result({
                        "run_id": run_id, "source_type": source_type, "source_id": source_id,
                        "persona": persona, "expected_code": expected_code, "final_code": None,
                        "final_rank": None,
                        "gold_in_top1": False, "gold_in_top5": False, "error": str(exc),
                    })
                except Exception:  # noqa: BLE001 - the failure-recording write itself can fail too (a malformed row rejected by a DB constraint, or the backend briefly unreachable); swallow it so it doesn't also abort the remaining gold queries in this run. It's still counted locally via failed += 1 below, just without a guaranteed remote record.
                    pass
                failed += 1
    except Exception as exc:  # noqa: BLE001 - anything escaping the block above (e.g. get_gold_queries() itself) must still let the run reach a terminal status below, not strand it at "running"
        outer_exc = exc

    if outer_exc is not None:
        # An abort that hit after some gold queries already succeeded must
        # never read as "completed" — the remaining, unscored gold queries
        # would silently be missing from a run that looks like a clean pass.
        final_status = "partially_failed" if succeeded else "failed"
    elif succeeded and not failed:
        final_status = "completed"
    elif succeeded and failed:
        final_status = "partially_failed"
    else:
        final_status = "failed"

    error_summary = f"{failed} of {len(gold_queries)} gold queries failed" if failed else None
    if outer_exc is not None:
        prefix = f"{error_summary}; " if error_summary else ""
        error_summary = f"{prefix}run aborted early: {outer_exc}"

    # Deliberately NOT wrapped in a swallowing try/except: if this call itself
    # fails (e.g. the backend is genuinely down for the whole run), there is
    # nothing further execute_run can do to record a status anywhere, and
    # swallowing that too would erase the failure with zero signal at all.
    await client.update_run(
        run_id,
        status=final_status,
        completed_at=datetime.now(timezone.utc).isoformat(),
        error_summary=error_summary,
    )
    return {"status": final_status, "succeeded": succeeded, "failed": failed}
