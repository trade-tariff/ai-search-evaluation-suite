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

After max_rounds answered questions the loop makes one terminal /searches
call with the full answer set and no extra answer: InteractiveSearchService
in trade-tariff-backend provides its forced best answer only once
answers.size >= max_rounds.

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

import uuid

from classification_core.qa_loop import SessionFacts, simulate_trader_answer


class QASessionError(RuntimeError):
    """Keep completed-round usage and trace when the terminal search fails."""

    def __init__(self, message: str, partial_result: dict):
        super().__init__(message)
        self.partial_result = partial_result


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
    question_trace: list[dict] = []
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
    # True until a round's usage explicitly says otherwise (AiUsage::PricingCalculator in
    # trade-tariff-backend sets pricing_known=False when a call used a model missing from
    # config/openai_model_pricing.yml). A round with no usage key at all -- no LLM call made --
    # makes no claim either way, so it leaves this unchanged rather than defaulting to False.
    pricing_known = True

    def usage_totals() -> dict:
        return {
            "cost_usd": total_cost_usd,
            "latency_seconds": total_duration_ms / 1000,
            "provider_calls": total_provider_calls,
            "pricing_known": pricing_known,
            # len(answers_so_far), not round_num -- round_num counts every
            # /searches call including the one that finally converges (which
            # answers nothing), so it overcounts by one on convergence.
            "questions_answered": len(answers_so_far),
            "question_trace": question_trace,
        }

    for round_num in range(1, max_rounds + 1):
        # Generated once per round, before the search call it tags — a UUID the backend's own
        # Api::Internal::SearchService accepts and threads through every log line that call
        # produces (confirmed by reading the service), so the admin app's drill-in page can later
        # link straight into Search Diagnostics for this exact round.
        request_id = str(uuid.uuid4())
        response = await client.search(query=query, answers_so_far=answers_so_far, run_time_overrides=run_time_overrides, request_id=request_id)
        usage = (response.get("meta") or {}).get("usage")
        if usage:
            total_cost_usd += usage.get("total_cost_usd") or 0
            total_duration_ms += usage.get("duration_ms") or 0
            total_provider_calls += usage.get("provider_calls") or 0
            pricing_known = pricing_known and usage.get("pricing_known", True)

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
            model=run_time_overrides.get("simulator_model"),
        )
        # Recorded whether or not the simulator succeeded — a failed round is exactly the round an
        # operator investigating a bad or incomplete result most needs to see, not a gap in the trace.
        question_trace.append({
            "round": round_num,
            "question": pending["question"],
            "options": options,
            "chosen": sim_result.get("chosen"),
            "choice_index": sim_result.get("choice_index"),
            "reasoning": sim_result.get("reasoning"),
            "attempts": sim_result.get("attempts"),
            "simulator_failed": sim_result.get("simulator_failed", False),
            "request_id": request_id,
        })
        if sim_result.get("simulator_failed"):
            return {"final_candidates": response.get("data") or [], "converged": False, "simulator_failed": True, **usage_totals()}

        answers_so_far.append({"question": pending["question"], "answer": sim_result["chosen"], "options": options})

    # Terminal call. The loop submits round r with the r-1 answers collected
    # so far, so after max_rounds rounds it has answered every allowed question
    # but never requested the final answer. InteractiveSearchService provides
    # its forced best answer only once answers.size >= max_rounds, so the
    # contract for getting that answer is one more /searches call with the
    # full answer set and no extra answer. A degenerate empty loop
    # (max_rounds <= 0) has no answers to submit, so it keeps the old shape.
    if answers_so_far:
        request_id = str(uuid.uuid4())
        try:
            response = await client.search(
                query=query, answers_so_far=answers_so_far,
                run_time_overrides=run_time_overrides, request_id=request_id,
            )
        except Exception as exc:
            raise QASessionError(str(exc), {
                "final_candidates": [], "converged": False, "simulator_failed": False,
                "forced_answer_request_id": request_id,
                **usage_totals(),
                # The failed request may have incurred unreported provider cost.
                "pricing_known": False,
            }) from exc
        usage = (response.get("meta") or {}).get("usage")
        if usage:
            total_cost_usd += usage.get("total_cost_usd") or 0
            total_duration_ms += usage.get("duration_ms") or 0
            total_provider_calls += usage.get("provider_calls") or 0
            pricing_known = pricing_known and usage.get("pricing_known", True)

        if _pending_question(response) is None:
            return {
                "final_candidates": response.get("data") or [],
                "converged": True,
                "simulator_failed": False,
                "forced_answer_request_id": request_id,
                **usage_totals(),
            }

        # Residual nonconvergence: even with max_rounds answers on record the
        # backend asked another question instead of a forced best answer (for
        # example a max_rounds override that disagrees with the loop's own).
        # Reported as converged=False with the terminal request_id for drill-in.
        return {
            "final_candidates": response.get("data") or [],
            "converged": False,
            "simulator_failed": False,
            "forced_answer_request_id": request_id,
            **usage_totals(),
        }
    return {"final_candidates": response.get("data") or [], "converged": False, "simulator_failed": False, **usage_totals()}
