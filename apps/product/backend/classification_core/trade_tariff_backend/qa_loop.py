"""Backend-driven Q&A orchestrator. Same loop shape as the existing
classification_core.qa_loop.run_qa_session (ask -> simulate answer -> feed
back -> repeat), but each round calls TradeTariffBackendClient.search instead of local
classification.classify_step, and convergence is detected from
meta.interactive_search.answers[-1].answer is None (confirmed by the
2026-08-14 spike and by reading search_service.rb#build_answers_list) instead
of a local `mode` field.

simulate_trader_answer and SessionFacts are imported UNCHANGED from the
existing classification_core.qa_loop — they never touched the database
(oracle_text is already a plain string parameter), so there is nothing to
adapt about them.

On simulator failure: simulate_trader_answer exhausts its own internal
retries (SIMULATOR_MAX_RETRIES) before returning simulator_failed=True with
chosen=None. The loop below stops immediately in that case rather than
substituting a fabricated answer (e.g. options[0]) and continuing — the
existing classification_core.qa_loop.run_qa_session does the same thing
(returns a distinct final_mode="simulator_failed" rather than guessing), and
for an eval suite this matters more than usual: a silently fabricated answer
would corrupt the gold_in_top1/gold_in_top5 metrics this whole plan exists to
produce, with nothing in the result to show it happened.
"""
from __future__ import annotations

from classification_core.qa_loop import SessionFacts, simulate_trader_answer


def _pending_question(search_response: dict) -> dict | None:
    turns = (search_response.get("meta") or {}).get("interactive_search", {}).get("answers") or []
    if turns and turns[-1].get("answer") is None:
        return turns[-1]
    return None


async def run_qa_session_via_trade_tariff_backend(
    client, sim_client, query: str, oracle_text: str, run_time_overrides: dict, max_rounds: int,
) -> dict:
    session = SessionFacts()
    answers_so_far: list[dict] = []
    response: dict = {}
    # meta.usage is present per /searches round (see SearchesController#with_usage_meta
    # in trade-tariff-backend) only when that round made at least one LLM/embedding
    # call -- absent, not zeroed, on a short-circuit round. Accumulated across every
    # round of this gold query's Q&A session, not just the last one, since a
    # duplicate-question-retry round or a later clarifying-question round can each
    # carry their own separate cost.
    total_cost_usd = 0.0
    total_duration_ms = 0.0
    total_provider_calls = 0

    def usage_totals() -> dict:
        return {
            "cost_usd": total_cost_usd,
            "latency_seconds": total_duration_ms / 1000,
            "provider_calls": total_provider_calls,
        }

    for round_num in range(1, max_rounds + 1):
        response = await client.search(query=query, answers_so_far=answers_so_far, run_time_overrides=run_time_overrides)
        usage = (response.get("meta") or {}).get("usage")
        if usage:
            total_cost_usd += usage.get("total_cost_usd") or 0
            total_duration_ms += usage.get("duration_ms") or 0
            total_provider_calls += usage.get("provider_calls") or 0

        pending = _pending_question(response)

        if pending is None:
            return {"final_candidates": response.get("data") or [], "converged": True, "simulator_failed": False, **usage_totals()}

        if sim_client is None:
            # No simulator configured (the CLASSIFICATION_ALLOW_PROVIDER_CALLS /
            # OPENAI_API_KEY spend gate is off). Fail here rather than calling
            # simulate_trader_answer with a None client, which would burn every
            # SIMULATOR_MAX_RETRIES attempt on an AttributeError before failing
            # for a misleading reason. Mirrors the `sim_client is not None`
            # guard in the existing classification_core.qa_loop.
            return {"final_candidates": response.get("data") or [], "converged": False, "simulator_failed": True, **usage_totals()}

        options = pending.get("options") or ["Yes"]
        sim_result = await simulate_trader_answer(
            client=sim_client, session=session, raw_query=query,
            question=pending["question"], options=options, round_number=round_num, oracle_text=oracle_text,
        )
        if sim_result.get("simulator_failed"):
            return {"final_candidates": response.get("data") or [], "converged": False, "simulator_failed": True, **usage_totals()}

        answers_so_far.append({"question": pending["question"], "answer": sim_result["chosen"], "options": options})

    return {"final_candidates": response.get("data") or [], "converged": False, "simulator_failed": False, **usage_totals()}
