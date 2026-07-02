"""Small capped Q&A comparison over a fixed retrieval shortlist.

This runner is intentionally separate from run_hydrated_e2e_matrix.py:
- retrieval is fixed to one matrix row
- question modes are compared side-by-side
- hard-prune, conservative-prune, and score-only policy outcomes are stored
  in final_state.policy_eval for each result row
- LLM trader answers are cached per gold row + option list so identical
  option sets get identical answers across modes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from . import classification, qa_loop
from .provider_guard import openai_allowed
from .run_hydrated_e2e_matrix import (
    DEFAULT_PERSONAS,
    DSN,
    _candidate_code,
    _flat,
    _rank_of,
    choose_gold_option,
    create_run,
    ensure_tables,
    finalize_run,
    insert_result,
    load_gold_rows,
)

QUESTION_MODES = [
    "facet_rules",
    "facet_rules_llm_wording",
    "llm_generated",
    "staging_eliminate",
]


def _ranked_codes(candidates: list[dict]) -> list[str]:
    return [_candidate_code(c) for c in candidates if _candidate_code(c)]


def _rank_summary(codes: list[str], expected_code: str) -> dict[str, Any]:
    rank = _rank_of(codes, expected_code)
    return {
        "rank": rank,
        "kept": rank is not None,
        "top1": rank == 1,
        "top_codes": codes[:50],
        "active_count": len(codes),
    }


def _selected_codes_from_meta(meta: dict | None) -> list[str]:
    if not meta:
        return []
    value = str(meta.get("value") or "").lower()
    if value in {"__none__", "none"} or "none of these" in str(meta.get("label") or "").lower():
        return []
    out = []
    for raw in meta.get("codes") or []:
        code = _flat(str(raw))
        if code:
            out.append(code)
    return out


def _policy_eval(candidates: list[dict], expected_code: str, trace: list[dict], hard_codes: list[str]) -> dict[str, Any]:
    original_codes = _ranked_codes(candidates)
    original_by_flat = {_flat(code): code for code in original_codes}
    original_index = {_flat(code): idx for idx, code in enumerate(original_codes)}

    score: dict[str, float] = {_flat(code): 0.0 for code in original_codes}
    conservative = list(candidates)
    conservative_trace: list[dict[str, Any]] = []

    for turn in trace:
        selected = set(_selected_codes_from_meta(turn.get("answer_meta")))
        if not selected:
            conservative_trace.append({
                "round": turn.get("round"),
                "action": "kept_all",
                "reason": "none_or_unmapped_answer",
            })
            continue
        for code in selected:
            if code in score:
                score[code] += 1.0

        matched = [c for c in conservative if _flat(_candidate_code(c)) in selected]
        ratio = len(matched) / max(1, len(conservative))
        # Conservative policy: only hard-prune when the selected bucket is a
        # meaningful discriminator, not a singleton or almost the whole set.
        if len(matched) >= 2 and ratio <= 0.85:
            conservative_trace.append({
                "round": turn.get("round"),
                "action": "filtered",
                "before": len(conservative),
                "after": len(matched),
                "ratio": round(ratio, 4),
            })
            conservative = matched
        else:
            conservative_trace.append({
                "round": turn.get("round"),
                "action": "kept_all",
                "before": len(conservative),
                "matched": len(matched),
                "ratio": round(ratio, 4),
                "reason": "weak_or_over_narrow_bucket",
            })

    score_order_flat = sorted(
        [_flat(code) for code in original_codes],
        key=lambda code: (-score.get(code, 0.0), original_index.get(code, 10**9)),
    )
    score_codes = [original_by_flat[code] for code in score_order_flat if code in original_by_flat]
    conservative_codes = _ranked_codes(conservative)
    return {
        "hard_prune": _rank_summary(hard_codes, expected_code),
        "conservative_prune": {
            **_rank_summary(conservative_codes, expected_code),
            "trace": conservative_trace,
        },
        "score_only": {
            **_rank_summary(score_codes, expected_code),
            "max_score": max(score.values()) if score else 0.0,
            "positive_scored_candidates": sum(1 for value in score.values() if value > 0),
        },
    }


@dataclass
class EvalContext:
    run_label: str
    retrieval_run_label: str
    retrieval_config: dict[str, Any]
    api_key: str | None
    retrieval_limit: int
    hydrate_limit: int
    max_rounds: int
    question_mode: str
    question_model: str
    staging_prompt_mode: str
    answerer: str
    allow_spend: bool
    simulator_client: Any | None
    answer_cache: dict[str, dict]


def _answer_cache_key(gold_id: int, options: list[str]) -> str:
    return json.dumps({"gold_id": gold_id, "options": list(options)}, sort_keys=True)


def _session_fact_authority_tier(gold: dict, answerer: str) -> int:
    """Authority follows the evidence source, not the qna_session capture path.

    Trader/human assertions remain low-authority by default. ATAR-backed oracle
    answers are treated like ATAR-derived extracted facts, not like canonical
    legal rules.
    """
    source_type = str(gold.get("source_type") or "").lower()
    source_id = str(gold.get("source_id") or "").lower()
    if answerer == "llm_trader" and (source_type == "atar" or source_id.startswith("atar")):
        return 5
    return 8


async def _choose_answer(
    *,
    gold: dict,
    ctx: EvalContext,
    session: qa_loop.SessionFacts,
    question_hint: dict,
    round_number: int,
) -> tuple[str, dict | None, dict, int]:
    options = [str(opt) for opt in (question_hint.get("options") or [])]
    cache_key = _answer_cache_key(int(gold["id"]), options)
    if ctx.answerer == "llm_trader" and cache_key in ctx.answer_cache:
        cached = dict(ctx.answer_cache[cache_key])
        answer = str(cached.get("answer") or "")
        if answer:
            session.record(
                slot=str(cached.get("slot") or f"cached_round_{round_number}"),
                answer=answer,
                source_question=str(question_hint.get("question") or ""),
                round_number=round_number,
            )
        meta = None
        for item in question_hint.get("options_meta") or []:
            if str(item.get("label") or "") == answer:
                meta = item
                break
        return answer, meta, {"source": "answer_cache", **cached}, 0

    if ctx.answerer == "llm_trader":
        if ctx.simulator_client is None:
            raise RuntimeError("llm_trader requires provider client")
        from .run_hydrated_e2e_matrix import choose_llm_trader_answer

        answer, meta, debug = await choose_llm_trader_answer(
            client=ctx.simulator_client,
            session=session,
            query=str(gold["query"]),
            oracle_text=str(gold.get("oracle_text") or ""),
            question_hint=question_hint,
            round_number=round_number,
        )
        ctx.answer_cache[cache_key] = {
            "answer": answer,
            "slot": debug.get("slot"),
            "reasoning": debug.get("reasoning"),
            "choice_index": debug.get("choice_index"),
        }
        return answer, meta, debug, 1

    answer, meta, debug = choose_gold_option(question_hint, str(gold["expected_code"]))
    if answer:
        session.record(
            slot=str(question_hint.get("facet_key") or question_hint.get("signal_key") or f"round_{round_number}"),
            answer=answer,
            source_question=str(question_hint.get("question") or ""),
            round_number=round_number,
        )
    return answer, meta, debug, 0


async def _evaluate_hydration_mode(gold: dict, ctx: EvalContext, candidates: list[dict]) -> dict:
    import main as product_main

    qa_history: list[dict] = []
    question_trace: list[dict] = []
    session = qa_loop.SessionFacts()
    provider_calls = 0
    final_hydration: dict[str, Any] | None = None

    for round_number in range(1, ctx.max_rounds + 1):
        payload = {
            "query": gold["query"],
            "candidates": candidates,
            "candidate_limit": len(candidates),
            "hydrate_limit": min(ctx.hydrate_limit, len(candidates)),
            "question_mode": ctx.question_mode,
            "qa_history": qa_history,
            "allow_spend": bool(ctx.allow_spend and ctx.question_mode != "facet_rules"),
            "config": {"question_wording_model": ctx.question_model},
        }
        hydration = await product_main.api_hydration_candidates(payload)
        final_hydration = hydration
        state = hydration.get("qa_state") or {}
        active_codes = [str(code) for code in (state.get("in_scope_codes") or [])]
        if not active_codes or len(active_codes) <= 1:
            break
        question_hint = hydration.get("question_hint") or {}
        options = question_hint.get("options") or []
        if not question_hint or not options:
            break

        answer, meta, answer_debug, answer_calls = await _choose_answer(
            gold=gold,
            ctx=ctx,
            session=session,
            question_hint=question_hint,
            round_number=round_number,
        )
        provider_calls += answer_calls
        if not answer:
            break
        turn = {
            "question": question_hint.get("question"),
            "answer": answer,
            "facet_key": question_hint.get("facet_key"),
            "signal_key": question_hint.get("signal_key"),
            "answer_value": (meta or {}).get("value"),
            "options_meta": question_hint.get("options_meta") or [],
            "mode": question_hint.get("mode") or ctx.question_mode,
        }
        qa_history.append(turn)
        question_trace.append({
            "round": round_number,
            "question": question_hint.get("question"),
            "mode": question_hint.get("mode"),
            "requested_mode": question_hint.get("requested_mode"),
            "source": question_hint.get("source"),
            "signal_key": question_hint.get("signal_key"),
            "options": options,
            "answer": answer,
            "answer_meta": meta,
            "answer_debug": answer_debug,
            "active_before": active_codes[:50],
        })
        if question_hint.get("provider_used"):
            provider_calls += 1

    final_payload = {
        "query": gold["query"],
        "candidates": candidates,
        "candidate_limit": len(candidates),
        "hydrate_limit": min(ctx.hydrate_limit, len(candidates)),
        "question_mode": "facet_rules",
        "qa_history": qa_history,
        "allow_spend": False,
        "config": {},
    }
    final_hydration = await product_main.api_hydration_candidates(final_payload)
    state = final_hydration.get("qa_state") or {}
    hard_codes = [str(code) for code in (state.get("in_scope_codes") or [])]
    policy_eval = _policy_eval(candidates, str(gold["expected_code"]), question_trace, hard_codes)
    persisted_session_facts = qa_loop.persist_session_facts_to_kg(
        commodity_code=str(gold["expected_code"]),
        facts=[f.__dict__ for f in session.snapshot().values()],
        source=f"qna_session:e2e:{ctx.run_label}:{ctx.question_mode}:{gold['id']}",
        provenance={
            "runner": "run_qna_mode_comparison",
            "run_label": ctx.run_label,
            "question_mode": ctx.question_mode,
            "answerer": ctx.answerer,
            "gold_id": gold["id"],
            "source_id": gold.get("source_id"),
            "persona": gold.get("persona"),
            "query": gold.get("query"),
            "source_authority_tier": _session_fact_authority_tier(gold, ctx.answerer),
        },
        authority_tier=_session_fact_authority_tier(gold, ctx.answerer),
    )
    return {
        "post_qa_rank": policy_eval["hard_prune"]["rank"],
        "post_qa_top_codes": hard_codes[:50],
        "gold_kept": policy_eval["hard_prune"]["kept"],
        "gold_top1_after_qa": policy_eval["hard_prune"]["top1"],
        "rounds": len(qa_history),
        "active_count": len(hard_codes),
        "out_count": int(state.get("out_of_scope_count") or 0),
        "cache_hit_count": int(final_hydration.get("cache_hit_count") or 0),
        "cache_write_count": int(final_hydration.get("cache_write_count") or 0),
        "provider_calls_used": provider_calls,
        "question_trace": question_trace,
        "final_state": {
            "qa_state": state,
            "coverage_totals": final_hydration.get("coverage_totals") or {},
            "lexical_specificity": final_hydration.get("lexical_specificity") or {},
            "query_difficulty": final_hydration.get("query_difficulty") or {},
            "active_query_difficulty": final_hydration.get("active_query_difficulty") or {},
            "policy_eval": policy_eval,
            "persisted_session_facts": persisted_session_facts,
        },
    }


async def _evaluate_staging_eliminate(gold: dict, ctx: EvalContext, candidates: list[dict]) -> dict:
    loop = asyncio.get_running_loop()
    qa_history: list[dict] = []
    session = qa_loop.SessionFacts()
    trace: list[dict] = []
    staging_debug: list[dict] = []
    provider_calls = 0
    final_codes = _ranked_codes(candidates)
    final_answers: list[dict] = []
    config = {
        "strategy": "eliminate",
        "use_llm_candidate_selection": True,
        "candidate_selection_model": ctx.question_model,
        "qa_mode": "ask_first",
        "prompt_mode": ctx.staging_prompt_mode,
        "use_facets": True,
        "use_session_facts": True,
        "use_entropy_picker": True,
        "use_llm_question_wording": True,
        "question_wording_model": ctx.question_model,
    }
    for round_number in range(1, ctx.max_rounds + 1):
        turn = await loop.run_in_executor(
            None,
            lambda: classification.eliminate_step(str(gold["query"]), qa_history, candidates, config),
        )
        if round_number > 1 and ctx.allow_spend:
            provider_calls += 1
        mode = turn.get("mode")
        augmentation = turn.get("augmentation_summary") or {}
        candidate_selection = augmentation.get("candidate_selection") or {}
        staging_debug.append({
            "round": round_number,
            "mode": mode,
            "candidate_selection": candidate_selection,
            "llm_response": turn.get("llm_response"),
            "eliminate_trace": turn.get("eliminate_trace"),
            "augmentation_debug": augmentation.get("debug"),
        })
        survivors = turn.get("survivors_all") or turn.get("answers") or []
        if survivors:
            final_answers = survivors
            final_codes = [str(item.get("commodity_code") or "") for item in survivors if item.get("commodity_code")]
        if mode == "answers":
            break
        question_obj = turn.get("question") or {}
        options = [str(opt) for opt in (question_obj.get("options") or [])]
        question_text = str(question_obj.get("question") or "")
        if mode != "questions" or not question_text or not options:
            break
        answer, meta, answer_debug, answer_calls = await _choose_answer(
            gold=gold,
            ctx=ctx,
            session=session,
            question_hint={"question": question_text, "options": options, "options_meta": []},
            round_number=round_number,
        )
        provider_calls += answer_calls
        if not answer:
            break
        qa_history.append({"question": question_text, "answer": answer})
        trace.append({
            "round": round_number,
            "question": question_text,
            "mode": "staging_eliminate",
            "requested_mode": "staging_eliminate",
            "source": "classification.eliminate_step",
            "prompt_mode": ctx.staging_prompt_mode,
            "candidate_selection": candidate_selection,
            "options": options,
            "answer": answer,
            "answer_meta": meta,
            "answer_debug": answer_debug,
            "active_before": final_codes[:50],
        })
    rank = _rank_of(final_codes, str(gold["expected_code"]))
    persisted_session_facts = qa_loop.persist_session_facts_to_kg(
        commodity_code=str(gold["expected_code"]),
        facts=[f.__dict__ for f in session.snapshot().values()],
        source=f"qna_session:e2e:{ctx.run_label}:{ctx.question_mode}:{ctx.staging_prompt_mode}:{gold['id']}",
        provenance={
            "runner": "run_qna_mode_comparison",
            "run_label": ctx.run_label,
            "question_mode": ctx.question_mode,
            "staging_prompt_mode": ctx.staging_prompt_mode,
            "answerer": ctx.answerer,
            "gold_id": gold["id"],
            "source_id": gold.get("source_id"),
            "persona": gold.get("persona"),
            "query": gold.get("query"),
            "source_authority_tier": _session_fact_authority_tier(gold, ctx.answerer),
        },
        authority_tier=_session_fact_authority_tier(gold, ctx.answerer),
    )
    return {
        "post_qa_rank": rank,
        "post_qa_top_codes": final_codes[:50],
        "gold_kept": rank is not None,
        "gold_top1_after_qa": rank == 1,
        "rounds": len(qa_history),
        "active_count": len(final_codes),
        "out_count": max(0, len(candidates) - len(final_codes)),
        "cache_hit_count": 0,
        "cache_write_count": 0,
        "provider_calls_used": provider_calls,
        "question_trace": trace,
        "final_state": {
            "qa_state": {"qa_history": qa_history, "in_scope_codes": final_codes, "in_scope_count": len(final_codes)},
            "policy_eval": {
                "staging_eliminate": _rank_summary(final_codes, str(gold["expected_code"])),
                "score_only": _rank_summary(final_codes, str(gold["expected_code"])),
                "conservative_prune": _rank_summary(final_codes, str(gold["expected_code"])),
            },
            "staging_prompt_mode": ctx.staging_prompt_mode,
            "staging_debug": staging_debug,
            "fallback_to_retrieval_rounds": sum(
                1 for item in staging_debug
                if ((item.get("candidate_selection") or {}).get("fallback_to_retrieval_order"))
            ),
            "final_answers": final_answers[:20],
            "persisted_session_facts": persisted_session_facts,
        },
    }


async def evaluate_row(gold: dict, ctx: EvalContext) -> dict:
    from experiment_retrieval import retrieve_for_config

    started = time.time()
    result = {
        "gold_id": gold["id"],
        "source_id": gold["source_id"],
        "persona": gold["persona"],
        "query": gold["query"],
        "expected_code": gold["expected_code"],
        "retrieval_run_label": ctx.retrieval_run_label,
        "question_mode": ctx.question_mode,
        "answerer": ctx.answerer,
        "question_trace": [],
        "provider_calls_used": 0,
    }
    try:
        candidates, leg_counts = retrieve_for_config(gold["query"], dict(ctx.retrieval_config), ctx.api_key, ctx.retrieval_limit)
        candidates = [dict(row) for row in candidates[:ctx.retrieval_limit]]
        for idx, cand in enumerate(candidates, start=1):
            cand.setdefault("rank", idx)
        top_codes = _ranked_codes(candidates)
        initial_rank = _rank_of(top_codes, str(gold["expected_code"]))
        result.update({
            "initial_rank": initial_rank,
            "initial_top_codes": top_codes[:50],
            "gold_in_retrieval": initial_rank is not None,
        })
        if not candidates:
            result["error"] = "no retrieval candidates"
            return result
        if ctx.question_mode == "staging_eliminate":
            extra = await _evaluate_staging_eliminate(gold, ctx, candidates)
        else:
            extra = await _evaluate_hydration_mode(gold, ctx, candidates)
        result.update(extra)
        result["final_state"]["leg_counts"] = leg_counts
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        result["latency_seconds"] = round(time.time() - started, 3)
    return result


def _max_calls_per_input(mode: str, answerer: str, max_rounds: int) -> int:
    answer_calls = max_rounds if answerer == "llm_trader" else 0
    question_calls = max_rounds if mode in {"facet_rules_llm_wording", "llm_generated", "staging_eliminate"} else 0
    return answer_calls + question_calls


async def run_mode(conn, *, run_label: str, gold_rows: list[dict], retrieval_run_label: str,
                   retrieval_config: dict, retrieval_limit: int, hydrate_limit: int,
                   max_rounds: int, question_mode: str, question_model: str,
                   answerer: str, allow_spend: bool, pair_limit: int,
                   personas: list[str], concurrency: int, answer_cache: dict[str, dict],
                   staging_prompt_mode: str,
                   estimated_call_cost_usd: float, cost_cap_usd: float) -> dict:
    if answerer == "llm_trader":
        if not allow_spend or not openai_allowed() or not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("llm_trader requires --allow-spend plus CLASSIFICATION_ALLOW_PROVIDER_CALLS=1 and OPENAI_API_KEY")
        from openai import AsyncOpenAI
        qa_loop.SIMULATOR_MODEL = os.environ.get("QA_SIMULATOR_MODEL", "gpt-5-mini")
        simulator_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
        simulator_model = qa_loop.SIMULATOR_MODEL
    else:
        simulator_client = None
        simulator_model = ""

    estimated_max_calls = len(gold_rows) * _max_calls_per_input(question_mode, answerer, max_rounds)
    estimated_max_cost = round(estimated_max_calls * estimated_call_cost_usd, 4)
    if allow_spend and estimated_max_cost > cost_cap_usd:
        raise SystemExit(
            f"estimated mode cost ${estimated_max_cost:.2f} exceeds cap ${cost_cap_usd:.2f} for {question_mode}"
        )

    run_id = create_run(
        conn,
        run_label=run_label,
        retrieval_run_label=retrieval_run_label,
        question_mode=question_mode,
        answerer=answerer,
        question_model=question_model,
        simulator_model=simulator_model,
        pair_limit=pair_limit,
        persona_count=len(personas),
        input_count=len(gold_rows),
        retrieval_limit=retrieval_limit,
        hydrate_limit=hydrate_limit,
        max_rounds=max_rounds,
        allow_spend=allow_spend,
        config={
            "retrieval_config": retrieval_config,
            "personas": personas,
            "estimated_max_provider_calls": estimated_max_calls,
            "estimated_max_cost_usd": estimated_max_cost,
            "estimated_call_cost_usd": estimated_call_cost_usd,
            "cost_cap_usd": cost_cap_usd,
            "policy_eval": ["hard_prune", "conservative_prune", "score_only"],
            "staging_prompt_mode": staging_prompt_mode,
            "classify_reasoning_effort": os.environ.get("CLASSIFY_REASONING_EFFORT"),
        },
    )
    ctx = EvalContext(
        run_label=run_label,
        retrieval_run_label=retrieval_run_label,
        retrieval_config=retrieval_config,
        api_key=os.environ.get("OPENAI_API_KEY"),
        retrieval_limit=retrieval_limit,
        hydrate_limit=hydrate_limit,
        max_rounds=max_rounds,
        question_mode=question_mode,
        question_model=question_model,
        staging_prompt_mode=staging_prompt_mode,
        answerer=answerer,
        allow_spend=allow_spend,
        simulator_client=simulator_client,
        answer_cache=answer_cache,
    )
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def process(gold: dict) -> dict:
        async with sem:
            return await evaluate_row(gold, ctx)

    tasks = [process(gold) for gold in gold_rows]
    for task in asyncio.as_completed(tasks):
        row = await task
        insert_result(conn, run_id, row)
        done += 1
        if done % 10 == 0 or done == len(tasks):
            print(json.dumps({
                "run_id": run_id,
                "question_mode": question_mode,
                "done": done,
                "total": len(tasks),
                "answer_cache_size": len(answer_cache),
            }), flush=True)
    summary = finalize_run(conn, run_id)
    print(json.dumps({"run_id": run_id, "summary": summary}, default=str), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run capped fixed-retrieval Q&A mode comparison")
    parser.add_argument("--run-label", default="qna_mode_comparison")
    parser.add_argument("--retrieval-run-label", default="all_legs_on_gpt54mini_scope_qna_plus_facts")
    parser.add_argument("--pair-limit", type=int, default=20)
    parser.add_argument("--personas", default=",".join(DEFAULT_PERSONAS))
    parser.add_argument("--retrieval-limit", type=int, default=100)
    parser.add_argument("--hydrate-limit", type=int, default=100)
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--question-modes", default=",".join(QUESTION_MODES))
    parser.add_argument("--question-model", default="gpt-5-mini")
    parser.add_argument("--staging-prompt-mode", default="baseline")
    parser.add_argument("--answerer", choices=["gold_option", "llm_trader"], default="llm_trader")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--allow-spend", action="store_true")
    parser.add_argument("--cost-cap-usd", type=float, default=30.0)
    parser.add_argument("--estimated-call-cost-usd", type=float, default=0.002)
    args = parser.parse_args()

    personas = [p.strip() for p in args.personas.split(",") if p.strip()]
    modes = [m.strip() for m in args.question_modes.split(",") if m.strip()]
    invalid = [m for m in modes if m not in QUESTION_MODES]
    if invalid:
        raise SystemExit(f"unknown question modes: {invalid}")

    from experiment_retrieval import experiment_requires_provider, select_experiment
    selected = select_experiment(args.retrieval_run_label)
    retrieval_config = dict(selected.get("config") or {})
    if experiment_requires_provider(args.retrieval_run_label) and not args.allow_spend:
        raise SystemExit("selected retrieval experiment requires provider calls; pass --allow-spend")

    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        ensure_tables(conn)
        gold_rows = load_gold_rows(conn, pair_limit=args.pair_limit, personas=personas)
        total_est_calls = sum(
            len(gold_rows) * _max_calls_per_input(mode, args.answerer, args.max_rounds)
            for mode in modes
        )
        total_est_cost = round(total_est_calls * args.estimated_call_cost_usd, 4)
        print(json.dumps({
            "gold_rows": len(gold_rows),
            "pair_limit": args.pair_limit,
            "personas": personas,
            "modes": modes,
            "retrieval_run_label": args.retrieval_run_label,
            "estimated_max_provider_calls": total_est_calls,
            "estimated_max_cost_usd": total_est_cost,
            "cost_cap_usd": args.cost_cap_usd,
        }), flush=True)
        if args.allow_spend and total_est_cost > args.cost_cap_usd:
            raise SystemExit(f"estimated total ${total_est_cost:.2f} exceeds cap ${args.cost_cap_usd:.2f}")

        answer_cache: dict[str, dict] = {}

        async def run_all_modes() -> None:
            for mode in modes:
                await run_mode(
                    conn,
                    run_label=args.run_label,
                    gold_rows=gold_rows,
                    retrieval_run_label=args.retrieval_run_label,
                    retrieval_config=retrieval_config,
                    retrieval_limit=args.retrieval_limit,
                    hydrate_limit=args.hydrate_limit,
                    max_rounds=args.max_rounds,
                    question_mode=mode,
                    question_model=args.question_model,
                    answerer=args.answerer,
                    allow_spend=args.allow_spend,
                    pair_limit=args.pair_limit,
                    personas=personas,
                    concurrency=args.concurrency,
                    answer_cache=answer_cache,
                    staging_prompt_mode=args.staging_prompt_mode,
                    estimated_call_cost_usd=args.estimated_call_cost_usd,
                    cost_cap_usd=args.cost_cap_usd,
                )

        asyncio.run(run_all_modes())


if __name__ == "__main__":
    main()
