from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from judge import (
    _extract_codes,
    _schema_valid_score,
    compute_consensus,
    compute_gold_metrics,
    detect_response_type,
    evaluate_pair,
)
from llm_judge import judge_response
from prompts import (
    build_prompt_messages,
    gold_is_retrievable,
    get_gold_code,
    get_gold_facts,
    get_oracle_text,
    get_raw_query,
)
from providers import clear_provider_cache, get_provider
from fact_store import FactStore
from schemas import (
    AppConfig,
    BenchmarkRun,
    CompletionResult,
    EvaluationResult,
    ModelConfig,
    ModelSummary,
    QARound,
    SimulatorConfig,
    SSEEvent,
)
from sections import section_for_code
from simulator import simulate_answer

# Matches production's interactive_search_max_questions default (7) rather
# than the 5 this harness used to impose, so the benchmark measures the same
# budget the live journey gives a trader.
MAX_ROUNDS = 7

# Verbatim from trade-tariff-backend
# app/services/interactive_search_service.rb (FINAL_ANSWER_INSTRUCTION).
# Production appends this and re-prompts once the question budget is spent;
# without it the model asks a question into the void and the harness records
# no answer at all, which then scores identically to a wrong answer.
FINAL_ANSWER_INSTRUCTION = (
    "\n\nIMPORTANT: You have asked the maximum number of questions allowed. "
    "Based on the search input, OpenSearch results, and the answers provided "
    "so far, you MUST now provide your best answer. Do not ask any more "
    "questions. Rank the opensearch results by confidence using the "
    "information you have.\n"
)


def _best_available_answers(prompt_index: int, opensearch_limit: int) -> str:
    """Deterministic last resort, ported from interactive_search_service.rb
    (best_available_answers): rank the retrieved candidates by their existing
    order, top two "Good" and the rest "Possible".

    Production can never return nothing, and neither should this. Reached only
    when the forced-answer re-prompt ALSO fails to produce answers.
    """
    from prompts import get_formatted_results

    results = get_formatted_results(prompt_index, opensearch_limit)[:5]
    answers = [
        {
            "commodity_code": r.get("commodity_code"),
            "confidence": "Good" if i < 2 else "Possible",
        }
        for i, r in enumerate(results)
        if r.get("commodity_code")
    ]
    return json.dumps({"answers": answers})


# No fan-out task should ever go this long without emitting an event. One
# that does is hung, and waiting on it forever stalls the whole run.
FANOUT_STALL_TIMEOUT_S = 420
JUDGE_TIMEOUT_S = 60
JUDGE_DRAIN_TIMEOUT_S = 300
RESULTS_DIR = Path(__file__).parent.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

_current_run: BenchmarkRun | None = None
# Kept beside _current_run so a checkpoint can recompute summaries without the
# run_benchmark closure. Summaries need model display names, nothing more.
_current_model_map: dict = {}
# Set when the user hits Stop; benchmark loop checks at drain boundaries and
# cancels in-flight asyncio tasks so no more provider/judge calls are made.
_cancel_event: asyncio.Event | None = None


_loaded_run_cache: tuple[str, "BenchmarkRun"] | None = None


def is_run_active() -> bool:
    """True only while a benchmark is genuinely in flight.

    Distinct from get_current_run(), which falls back to the newest saved run
    so the results endpoints survive a restart. That fallback made /status
    report a finished run as if it were live, so callers could never tell
    "nothing is running" from "the last run ended badly".
    """
    return _current_run is not None and _current_run.status == "running"


def get_current_run() -> BenchmarkRun | None:
    """The in-memory run, else the newest saved run (survives restarts -
    the results endpoint 404'd after every container rebuild otherwise)."""
    global _loaded_run_cache
    if _current_run is not None:
        return _current_run
    # Newest by mtime - run ids are random hex, so name order is meaningless.
    files = sorted(RESULTS_DIR.glob("benchmark_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        try:
            if _loaded_run_cache and _loaded_run_cache[0] == f.name:
                return _loaded_run_cache[1]
            run = BenchmarkRun(**json.loads(f.read_text()))
            _loaded_run_cache = (f.name, run)
            return run
        except Exception:
            continue
    return None


def cancel_current_run() -> bool:
    """Signal the currently running benchmark to stop. Returns True if a run
    was in progress and the signal was delivered. In-flight provider/judge
    calls may still complete (API calls are not interruptible mid-flight),
    but no new ones will be dispatched and the run will be marked cancelled."""
    global _cancel_event
    if _cancel_event is not None and not _cancel_event.is_set():
        _cancel_event.set()
        return True
    return False


def list_saved_runs() -> list[dict]:
    """List saved benchmark runs (metadata only)."""
    runs = []
    for f in sorted(RESULTS_DIR.glob("benchmark_*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            runs.append({
                "id": data["id"],
                "timestamp": data["timestamp"],
                "status": data["status"],
                "opensearch_limit": data.get("opensearch_limit", 80),
                "gold_mode": data.get("gold_mode", False),
                "baseline_model_id": data.get("baseline_model_id"),
                "panel_model_ids": data.get("panel_model_ids", []),
                "prompt_count": len(data.get("prompt_indices", [])),
                "model_count": len(data.get("model_ids", [])),
                "summary_count": len(data.get("summaries", [])),
                # Without this an interrupted run reads as empty in the list,
                # so an operator cannot tell "died immediately" from "died at
                # 90% with most of the results intact".
                "progress": data.get("progress", 0.0),
                "completion_count": len(data.get("model_results", []))
                + len(data.get("panel_results", [])),
                "filename": f.name,
            })
        except (json.JSONDecodeError, KeyError):
            continue
    return runs


def load_saved_run(run_id: str) -> BenchmarkRun | None:
    """Load a saved run by ID."""
    path = RESULTS_DIR / f"benchmark_{run_id}.json"
    if not path.exists():
        return None
    return BenchmarkRun.model_validate_json(path.read_text())


def _save_run(run: BenchmarkRun) -> None:
    """Write a run to disk atomically.

    Called on every completion, not just at the end, so a dropped client or a
    killed process leaves the work that was already paid for on disk. Writes
    to a temp file and renames, so a crash mid-write cannot leave a truncated
    JSON file where a valid earlier checkpoint used to be.
    """
    path = RESULTS_DIR / f"benchmark_{run.id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(run.model_dump_json(indent=2))
    tmp.replace(path)


def checkpoint_current_run(status: str | None = None) -> str | None:
    """Persist whatever the in-flight run has so far. Returns its id, or None
    if no run is in flight.

    The SSE generator that drives a benchmark is closed the moment the client
    disconnects, so anything after the last `yield` never executes. Without a
    checkpoint the entire run - every provider call already billed - is lost.
    """
    if _current_run is None:
        return None
    if status is not None:
        _current_run.status = status
    try:
        # Recompute summaries over whatever has completed. Without this an
        # interrupted run persists raw results but summary_count=0, so the
        # runs list makes it look empty and the operator bins work that was
        # already paid for.
        _current_run.summaries = compute_summaries(_current_run, _current_model_map)
    except Exception:
        pass
    try:
        _save_run(_current_run)
    except Exception:
        # Never let a checkpoint failure take down the run itself.
        return _current_run.id
    return _current_run.id


def _parse_questions(response_text: str) -> list[dict]:
    """Extract questions and options from an LLM response."""
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, dict) and "questions" in parsed:
            return parsed["questions"]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _extract_top_code(response_text: str) -> str | None:
    """Pull the strongest commodity_code from an 'answers' JSON response, for UI."""
    if not response_text:
        return None
    try:
        parsed = json.loads(response_text)
        if isinstance(parsed, dict) and parsed.get("answers"):
            first = parsed["answers"][0]
            if isinstance(first, dict):
                return first.get("commodity_code")
    except (json.JSONDecodeError, TypeError, KeyError, IndexError):
        pass
    return None


def _auto_answer(questions: list[dict]) -> list[dict]:
    """Legacy: pick options[0] for each question.

    Kept as the graceful-degradation path when the simulator is disabled or
    the OpenAI key is missing. The active path is _simulate_answers_for_round.
    """
    qa_entries = []
    for q in questions:
        question_text = q.get("question", "")
        options = q.get("options", [])
        answer = options[0] if options else "Yes"
        qa_entries.append({
            "question": question_text,
            "options": options,
            "answer": answer,
        })
    return qa_entries


async def _simulate_answers_for_round(
    simulator_client,
    simulator_config: SimulatorConfig,
    fact_store: FactStore,
    prompt_index: int,
    round_number: int,
    model_id: str,
    raw_query: str,
    questions: list[dict],
    oracle_text: str | None = None,
    event_bus: "asyncio.Queue | None" = None,
) -> tuple[list[dict], list[dict], float, float, int]:
    """Ask the fact-store-backed trader simulator to answer every question.

    Sibling questions serialise through the per-prompt lock so slot writes are
    visible to later sibling calls; this is a deliberate correctness choice
    (we do not want two concurrent calls inventing two different values for
    the same slot).

    `oracle_text` (if set, e.g. an ATaR ruling body) is passed to the
    simulator as the authoritative product description for any question the
    seeded facts don't cover.

    Returns (qa_entries, simulator_trace, total_cost, total_latency_ms,
             store_hit_count).
    """
    if not questions:
        return [], [], 0.0, 0.0, 0

    lock = await fact_store.lock_for(prompt_index)
    qa_entries: list[dict] = []
    trace: list[dict] = []
    total_cost = 0.0
    total_latency = 0.0
    store_hits = 0

    # Serialise sibling questions under the per-prompt lock so a fact committed
    # for question 1 is visible by the time question 2 is resolved.
    async with lock:
        for q in questions:
            question_text = q.get("question", "")
            options = q.get("options", []) or []
            sim = await simulate_answer(
                client=simulator_client,
                fact_store=fact_store,
                prompt_index=prompt_index,
                round_number=round_number,
                model_id=model_id,
                query=raw_query,
                question=question_text,
                options=options,
                config=simulator_config,
                oracle_text=oracle_text,
                event_bus=event_bus,
            )
            qa_entries.append({
                "question": question_text,
                "options": options,
                "answer": sim["chosen"] if options else "Yes",
            })
            trace.append({
                "question": question_text,
                "chosen": sim["chosen"],
                "slot": sim["slot"],
                "reasoning": sim["reasoning"],
                "consistent_with_prior": sim["consistent_with_prior"],
                "from_store": sim["from_store"],
                "cost": sim["cost"],
                "latency_ms": sim["latency_ms"],
            })
            total_cost += sim["cost"]
            total_latency += sim["latency_ms"]
            if sim["consistent_with_prior"]:
                store_hits += 1
    return qa_entries, trace, total_cost, total_latency, store_hits


def _none_safe_mean(values: list, digits: int) -> float | None:
    """Mean over non-None values, None if every input is None. Gold-mode evals
    leave every reference-agreement metric None ("not evaluated"), so the
    summary must surface None rather than a misleading 0.0."""
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), digits) if vals else None


def _evaluate_gold_only(
    result: CompletionResult, gold_code: str | None,
) -> EvaluationResult:
    """Gold-mode evaluation: there is no consensus reference, so every
    reference-agreement field is None and the candidate is scored against the
    prompt's gold code plus the deterministic quality signals that need no
    baseline (schema validity, Q&A efficiency)."""
    gold_metrics = compute_gold_metrics(_extract_codes(result.response_text), gold_code)

    total_questions = 0
    new_slots_set = 0
    for rnd in result.rounds:
        total_questions += len(rnd.questions_asked or [])
        for t in rnd.simulator_trace or []:
            if isinstance(t, dict) and not t.get("consistent_with_prior", False):
                new_slots_set += 1
    question_efficiency = (
        new_slots_set / total_questions if total_questions > 0 else 1.0
    )
    rounds_efficiency = 1.0 - max(
        0, min(result.total_rounds, MAX_ROUNDS) - 1
    ) / max(1, MAX_ROUNDS - 1)

    return EvaluationResult(
        model_id=result.model_id,
        prompt_index=result.prompt_index,
        cosine_similarity=None,
        code_match_score=None,
        top1_match=None,
        top3_hit=None,
        top5_overlap=None,
        mean_reciprocal_rank=None,
        heading_match=None,
        chapter_match=None,
        hierarchical_score=None,
        schema_valid=round(_schema_valid_score(result.response_text), 2),
        total_questions=total_questions,
        new_slots_set=new_slots_set,
        question_efficiency=round(question_efficiency, 4),
        rounds_efficiency=round(rounds_efficiency, 4),
        gold_code=str(gold_code) if gold_code else None,
        gold_top1_match=gold_metrics["gold_top1_match"],
        gold_heading_match=gold_metrics["gold_heading_match"],
        gold_chapter_match=gold_metrics["gold_chapter_match"],
        gold_hierarchical_score=gold_metrics["gold_hierarchical_score"],
        delta_score=None,
        panel_agreement=None,
        total_latency_ms=round(result.total_latency_ms, 1),
        baseline_total_latency_ms=None,
        speed_factor=None,
        total_cost=round(result.total_cost, 6),
        baseline_total_cost=None,
        total_rounds=result.total_rounds,
        baseline_total_rounds=None,
    )


async def _run_qa_loop(
    provider,
    model_config: ModelConfig,
    prompt_index: int,
    api_keys: dict,
    opensearch_limit: int = 80,
    simulator_client=None,
    simulator_config: SimulatorConfig | None = None,
    fact_store: FactStore | None = None,
    oracle_text: str | None = None,
    event_bus: "asyncio.Queue | None" = None,
    is_panel: bool = False,
) -> CompletionResult:
    """Run the full Q&A loop until confident answer or MAX_ROUNDS.

    When simulator_client + config + fact_store are all provided, clarifying
    answers come from the fact-store-backed trader simulator - consistent
    across all models for this prompt. Otherwise falls back to options[0].

    `oracle_text` (e.g. an ATaR ruling body) is forwarded to the simulator as
    an authoritative product description for any question the seeded fact
    sheet does not already cover.
    """
    qa_history: list[dict] = []
    rounds: list[QARound] = []
    total_latency = 0.0
    total_input = 0
    total_output = 0
    total_cost = 0.0
    total_sim_cost = 0.0
    total_sim_latency = 0.0
    total_sim_store_hits = 0
    final_text = ""
    final_type = "unknown"
    error = None

    raw_query = get_raw_query(prompt_index)
    use_simulator = (
        simulator_client is not None
        and simulator_config is not None
        and simulator_config.enabled
        and fact_store is not None
    )

    # One extra turn beyond the question budget, mirroring production: the
    # final turn is not another chance to ask, it is a forced answer.
    for round_num in range(1, MAX_ROUNDS + 2):
        forced = round_num > MAX_ROUNDS
        messages = build_prompt_messages(
            prompt_index,
            qa_history if qa_history else None,
            opensearch_limit=opensearch_limit,
        )
        if forced:
            messages[0]["content"] += FINAL_ANSWER_INSTRUCTION
        result = await provider.complete(messages, model_config, prompt_index)

        if result.error:
            error = result.error
            rounds.append(QARound(
                round_number=round_num,
                response_text="",
                response_type="error",
                latency_ms=result.latency_ms,
                cost=result.cost,
            ))
            total_latency += result.latency_ms
            total_cost += result.cost
            break

        resp_type = detect_response_type(result)
        questions_asked = _parse_questions(result.response_text)

        if forced:
            # The budget is spent. Anything that is not an answer becomes the
            # deterministic ranked fallback, so this path always yields a code.
            if resp_type != "answers":
                result.response_text = _best_available_answers(
                    prompt_index, opensearch_limit
                )
                resp_type = "answers"
            questions_asked = []

        # Push the questions live BEFORE the simulator answers them, so the
        # UI sees the model thinking out loud while the slow path runs.
        if event_bus is not None and questions_asked:
            try:
                event_bus.put_nowait((
                    "live",
                    "model:question",
                    {
                        "model_id": model_config.id,
                        "prompt_index": prompt_index,
                        "round_number": round_num,
                        "is_panel": is_panel,
                        "questions": questions_asked,
                    },
                ))
            except asyncio.QueueFull:
                pass

        if questions_asked and use_simulator:
            answers_given, sim_trace, sim_cost, sim_latency, sim_hits = (
                await _simulate_answers_for_round(
                    simulator_client=simulator_client,
                    simulator_config=simulator_config,
                    fact_store=fact_store,
                    prompt_index=prompt_index,
                    round_number=round_num,
                    model_id=model_config.id,
                    raw_query=raw_query,
                    questions=questions_asked,
                    oracle_text=oracle_text,
                    event_bus=event_bus,
                )
            )
        elif questions_asked:
            answers_given = _auto_answer(questions_asked)
            sim_trace, sim_cost, sim_latency, sim_hits = [], 0.0, 0.0, 0
        else:
            answers_given, sim_trace, sim_cost, sim_latency, sim_hits = (
                [], [], 0.0, 0.0, 0
            )

        rounds.append(QARound(
            round_number=round_num,
            response_text=result.response_text,
            response_type=resp_type,
            questions_asked=questions_asked,
            answers_given=answers_given,
            simulator_trace=sim_trace,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
            cost=result.cost,
            simulator_cost=round(sim_cost, 6),
            simulator_latency_ms=round(sim_latency, 1),
        ))

        # End-of-round live event: the UI uses this to advance the per-model
        # round counter without waiting for the full task to complete.
        if event_bus is not None:
            try:
                event_bus.put_nowait((
                    "live",
                    "model:round",
                    {
                        "model_id": model_config.id,
                        "prompt_index": prompt_index,
                        "round_number": round_num,
                        "is_panel": is_panel,
                        "response_type": resp_type,
                        "questions_asked": questions_asked,
                        "answers_given": answers_given,
                        "latency_ms": result.latency_ms,
                        "cost": result.cost,
                        "input_tokens": result.input_tokens,
                        "output_tokens": result.output_tokens,
                        "top_code": _extract_top_code(result.response_text),
                    },
                ))
            except asyncio.QueueFull:
                pass

        total_latency += result.latency_ms
        total_input += result.input_tokens
        total_output += result.output_tokens
        total_cost += result.cost
        total_sim_cost += sim_cost
        total_sim_latency += sim_latency
        total_sim_store_hits += sim_hits
        final_text = result.response_text
        final_type = resp_type

        if resp_type == "answers":
            break

        if resp_type == "questions" and answers_given:
            qa_history.extend(answers_given)
        else:
            break

    return CompletionResult(
        model_id=model_config.id,
        prompt_index=prompt_index,
        response_text=final_text,
        response_type=final_type,
        rounds=rounds,
        total_rounds=len(rounds),
        total_latency_ms=round(total_latency, 1),
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_cost=round(total_cost, 6),
        input_tokens=total_input,
        output_tokens=total_output,
        latency_ms=round(total_latency, 1),
        cost=round(total_cost, 6),
        total_simulator_cost=round(total_sim_cost, 6),
        total_simulator_latency_ms=round(total_sim_latency, 1),
        simulator_store_hits=total_sim_store_hits,
        error=error,
    )


def compute_summaries(run: BenchmarkRun, model_map: dict) -> list[ModelSummary]:
    """Aggregate per-model summaries from whatever the run has so far.

    Extracted from the end of run_benchmark so a checkpoint can call it too.
    An interrupted run used to persist its raw results with no summaries at
    all, so the runs list showed summary_count=0 and an operator would bin a
    run that actually held perfectly good, already-paid-for results.

    Safe to call mid-run: it aggregates over the evaluations recorded so far,
    so the numbers describe the completed subset, not the intended one.
    """
    out: list[ModelSummary] = []
    # ── Phase 4: Summaries ──
    evals_by_model: dict[str, list[EvaluationResult]] = defaultdict(list)
    for ev in run.evaluations:
        evals_by_model[ev.model_id].append(ev)

    # Index completion results by model_id so we can attribute simulator stats.
    # Consensus rows aggregate across all panel members.
    completions_by_model: dict[str, list[CompletionResult]] = defaultdict(list)
    for r in run.model_results:
        completions_by_model[r.model_id].append(r)
    for r in run.panel_results:
        completions_by_model[r.model_id].append(r)
        completions_by_model["consensus"].append(r)

    for mid, evals in evals_by_model.items():
        mc = model_map.get(mid)
        if mid == "consensus":
            bl_id = run.baseline_model_id or "panel"
            name = f"Consensus ({bl_id})"
        else:
            name = mc.name if mc else mid
        n = len(evals)
        if n == 0:
            continue

        # Judge now scores fact_consistency + question_quality only. Any
        # eval with a non-None fact_consistency had a successful judge call.
        judged_fc = [e for e in evals if e.judge_fact_consistency is not None]
        judged_qq = [e for e in evals if e.judge_question_quality is not None]
        jn = len(judged_fc)
        err_count = sum(1 for e in evals if e.judge_error)

        sim_completions = completions_by_model.get(mid, [])
        sim_total_cost = sum(c.total_simulator_cost for c in sim_completions)
        total_questions = sum(
            sum(len(r.questions_asked) for r in c.rounds) for c in sim_completions
        )
        total_store_hits = sum(c.simulator_store_hits for c in sim_completions)
        store_hit_rate = (
            round(total_store_hits / total_questions, 4) if total_questions else 0.0
        )

        # Gold-truth aggregates: only average over evals that had a gold_code.
        # A model with 0 gold-evaluated prompts gets gold_evaluated_count=0 and
        # None for all rates, which the UI renders as "-".
        gold_evals = [e for e in evals if e.gold_code is not None]
        gold_n = len(gold_evals)
        if gold_n > 0:
            gold_top1_rate = round(
                sum(1 for e in gold_evals if e.gold_top1_match) / gold_n, 4
            )
            gold_top3_rate = round(
                sum(1 for e in gold_evals if e.gold_top3_hit) / gold_n, 4
            )
            gold_top5_rate = round(
                sum(1 for e in gold_evals if e.gold_top5_hit) / gold_n, 4
            )
            avg_gold_rr = round(
                sum(e.gold_reciprocal_rank or 0.0 for e in gold_evals) / gold_n, 4
            )
            gold_heading_rate = round(
                sum(1 for e in gold_evals if e.gold_heading_match) / gold_n, 4
            )
            gold_chapter_rate = round(
                sum(1 for e in gold_evals if e.gold_chapter_match) / gold_n, 4
            )
            avg_gold_hier = round(
                sum(
                    e.gold_hierarchical_score or 0.0 for e in gold_evals
                ) / gold_n,
                4,
            )
        else:
            gold_top1_rate = None
            gold_top3_rate = None
            gold_top5_rate = None
            avg_gold_rr = None
            gold_heading_rate = None
            gold_chapter_rate = None
            avg_gold_hier = None

        scorable = [c for c in sim_completions if not c.error]

        # A code that is not 10 digits is malformed output, not a wrong
        # answer - gpt-5-mini once returned "691200". Both score as a miss
        # everywhere else, so count malformed separately or it hides.
        malformed_codes = []
        for c in scorable:
            for code in _extract_codes(c.response_text)[:1]:
                if not (code.isdigit() and len(code) == 10):
                    malformed_codes.append(code)

        # "No answer" = the completion carries no commodity code at all. That
        # is a different failure from a confidently wrong code (usually the
        # round cap ran out mid-questioning), but every accuracy metric above
        # scores the two identically, so count it on its own.
        no_answer_count = sum(
            1 for c in scorable if not _extract_codes(c.response_text)
        )
        no_answer_rate = (
            round(no_answer_count / len(scorable), 4) if scorable else None
        )

        # Reference-comparison boolean rates are only meaningful when at
        # least one eval was scored against a reference. In gold mode every
        # reference field (top1_match etc.) is None, so the rates are None
        # ("not evaluated"), not 0.0.
        has_reference = any(e.top1_match is not None for e in evals)

        summary = ModelSummary(
            model_id=mid,
            model_name=name,
            avg_cosine_similarity=_none_safe_mean([e.cosine_similarity for e in evals], 4),
            avg_code_match_score=_none_safe_mean([e.code_match_score for e in evals], 4),
            avg_delta_score=_none_safe_mean([e.delta_score for e in evals], 4),
            avg_total_latency_ms=round(sum(e.total_latency_ms for e in evals) / n, 1),
            avg_speed_factor=_none_safe_mean([e.speed_factor for e in evals], 3),
            total_cost=round(sum(e.total_cost for e in evals), 6),
            avg_cost_per_classification=round(sum(e.total_cost for e in evals) / n, 6),
            top1_accuracy=round(sum(1 for e in evals if e.top1_match) / n, 4) if has_reference else None,
            avg_top5_overlap=_none_safe_mean([e.top5_overlap for e in evals], 4),
            avg_rounds=round(sum(e.total_rounds for e in evals) / n, 2),
            heading_match_rate=round(sum(1 for e in evals if e.heading_match) / n, 4) if has_reference else None,
            chapter_match_rate=round(sum(1 for e in evals if e.chapter_match) / n, 4) if has_reference else None,
            top3_hit_rate=round(sum(1 for e in evals if e.top3_hit) / n, 4) if has_reference else None,
            avg_mean_reciprocal_rank=_none_safe_mean([e.mean_reciprocal_rank for e in evals], 4),
            avg_hierarchical_score=_none_safe_mean([e.hierarchical_score for e in evals], 4),
            avg_schema_valid=round(sum(e.schema_valid for e in evals) / n, 4),
            avg_question_efficiency=round(sum(e.question_efficiency for e in evals) / n, 4),
            avg_rounds_efficiency=round(sum(e.rounds_efficiency for e in evals) / n, 4),
            # Legacy judge dimensions are None (replaced by deterministic metrics)
            avg_judge_score=None,
            avg_judge_classification_accuracy=None,
            avg_judge_structured_output=None,
            avg_judge_fact_consistency=round(sum(e.judge_fact_consistency for e in judged_fc) / len(judged_fc), 2) if judged_fc else None,
            avg_judge_question_quality=round(sum(e.judge_question_quality for e in judged_qq) / len(judged_qq), 2) if judged_qq else None,
            judge_scored_count=jn,
            judge_error_count=err_count,
            total_judge_cost=round(sum(e.judge_cost for e in evals), 6),
            total_simulator_cost=round(sim_total_cost, 6),
            avg_simulator_store_hit_rate=store_hit_rate,
            gold_evaluated_count=gold_n,
            no_answer_count=no_answer_count,
            no_answer_rate=no_answer_rate,
            malformed_code_count=len(malformed_codes),
            malformed_codes=malformed_codes[:5],
            gold_top1_rate=gold_top1_rate,
            gold_top3_rate=gold_top3_rate,
            gold_top5_rate=gold_top5_rate,
            avg_gold_reciprocal_rank=avg_gold_rr,
            gold_heading_rate=gold_heading_rate,
            gold_chapter_rate=gold_chapter_rate,
            avg_gold_hierarchical_score=avg_gold_hier,
        )
        out.append(summary)
    return out


async def run_benchmark(
    prompt_indices: list[int],
    model_ids: list[str],
    config: AppConfig,
    opensearch_limit: int = 80,
    gold_mode: bool | None = None,
) -> AsyncIterator[SSEEvent]:
    global _current_run, _cancel_event, _current_model_map
    clear_provider_cache()
    _cancel_event = asyncio.Event()
    all_tasks: list[asyncio.Task] = []

    def check_cancelled() -> bool:
        """Call at drain boundaries. Returns True if run should abort."""
        if _cancel_event is not None and _cancel_event.is_set():
            for t in all_tasks:
                if not t.done():
                    t.cancel()
            return True
        return False

    run_id = uuid.uuid4().hex[:12]
    # Fresh fact store per run. Per-prompt scope is enforced inside FactStore.
    fact_store = FactStore()
    # Pre-seed the fact store with any user-approved gold facts (typically
    # extracted from ATaR rulings and approved in the AtarPanel UI). These land
    # before any model runs, so candidate Q&A hits a warm store for the most
    # common slots and burns no LLM calls re-deriving them.
    seeded_facts: dict[int, int] = {}
    for pi in prompt_indices:
        n = fact_store.seed(pi, get_gold_facts(pi))
        if n:
            seeded_facts[pi] = n
    # Per-prompt oracle text (e.g. ATaR ruling body) cached up-front so we
    # avoid re-reading the JSON file inside every Q&A loop.
    oracle_by_prompt: dict[int, str | None] = {
        pi: get_oracle_text(pi) for pi in prompt_indices
    }
    # Which prompts CAN be answered correctly at all. The prompt forbids
    # answering outside the retrieved candidates, so a prompt whose gold code
    # is missing from them caps every model at zero for that prompt.
    gold_retrievable = {
        str(pi): r
        for pi in prompt_indices
        if (r := gold_is_retrievable(pi, opensearch_limit)) is not None
    }
    # Gold mode: when every prompt carries a gold code, the reference panel,
    # consensus and LLM judge add no correctness signal - candidates are
    # scored against gold directly, which saves the panel + judge spend.
    # None = auto-detect; explicit True/False overrides.
    if gold_mode is None:
        gold_mode_effective = bool(prompt_indices) and all(
            bool(get_gold_code(pi)) for pi in prompt_indices
        )
    else:
        gold_mode_effective = gold_mode
    _current_run = BenchmarkRun(
        id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        status="running",
        opensearch_limit=opensearch_limit,
        gold_mode=gold_mode_effective,
        prompt_indices=prompt_indices,
        model_ids=model_ids,
        gold_retrievable=gold_retrievable,
    )

    api_keys = config.api_keys.model_dump()
    model_map: dict[str, ModelConfig] = {m.id: m for m in config.models}
    # Checkpoints recompute summaries outside this closure; they need names.
    _current_model_map = model_map

    # Reference is pinned via ReferenceConfig. Three modes:
    #   single     -> [ref_model]               (1 task per prompt)
    #   multi_pass -> [ref_model] * passes      (N tasks per prompt, same model)
    #   panel      -> [model_map[i] for i in panel_model_ids]
    #
    # All three feed into compute_consensus which handles pairwise similarity
    # + majority vote on final codes and emits a panel_agreement score. When
    # agreement is high, the reference is self-consistent. When low, the
    # prompt is genuinely ambiguous for the reference - useful signal.
    ref_cfg = config.reference_config
    panel_mcs: list[ModelConfig] = []
    ref_err: str | None = None

    if ref_cfg.mode == "single":
        ref_model = model_map.get(ref_cfg.model_id)
        if ref_model is None:
            ref_err = f"Reference model '{ref_cfg.model_id}' not found"
        else:
            panel_mcs = [ref_model]
    elif ref_cfg.mode == "multi_pass":
        ref_model = model_map.get(ref_cfg.model_id)
        if ref_model is None:
            ref_err = f"Reference model '{ref_cfg.model_id}' not found"
        elif ref_cfg.passes < 1:
            ref_err = f"multi_pass requires passes >= 1, got {ref_cfg.passes}"
        else:
            panel_mcs = [ref_model] * ref_cfg.passes
    elif ref_cfg.mode == "panel":
        panel_models = [model_map.get(mid) for mid in ref_cfg.panel_model_ids]
        missing = [
            mid for mid, m in zip(ref_cfg.panel_model_ids, panel_models) if m is None
        ]
        if missing:
            ref_err = f"Panel models not found: {missing}"
        elif len(panel_models) < 2:
            ref_err = "panel mode requires at least 2 models"
        else:
            panel_mcs = [m for m in panel_models if m is not None]
    else:
        ref_err = f"Unknown reference mode: {ref_cfg.mode!r}"

    if ref_err is not None:
        _current_run.status = "error"
        yield SSEEvent(event="error", data={"message": ref_err})
        return

    if gold_mode_effective:
        # No reference phase in gold mode - the panel is never launched. This
        # has to happen BEFORE the candidate dedup below: otherwise a model that
        # is also the reference is dropped from the candidates, and then the
        # panel it was dropped into is emptied, so it runs nowhere at all.
        panel_mcs = []

    # Candidates = user-selected minus any that are already in the reference set
    # (de-dupes the case where a user explicitly picks the reference model).
    panel_ids = {mc.id for mc in panel_mcs}
    candidate_mcs: list[ModelConfig] = []
    deduped_into_panel: list[str] = []
    for mid in model_ids:
        mc = model_map.get(mid)
        if mc is None:
            continue
        if mc.id in panel_ids:
            # Still runs, but as the reference - not as a scored candidate.
            # Surfaced to the UI below so its absence from the results table
            # is explained rather than silent.
            deduped_into_panel.append(mc.id)
            continue
        candidate_mcs.append(mc)

    if not panel_mcs and not candidate_mcs:
        _current_run.status = "error"
        yield SSEEvent(
            event="error",
            data={"message": "No models to run."},
        )
        return

    _current_run.panel_model_ids = [mc.id for mc in panel_mcs]
    _current_run.baseline_model_id = panel_mcs[0].id if panel_mcs else None  # backward compat
    panel_count = len(panel_mcs)
    candidate_count = len(candidate_mcs)
    total_tasks = len(prompt_indices) * (panel_count + candidate_count)
    completed = 0

    # Trader simulator: one shared client, used by every model's Q&A loop so
    # that answer selection is consistent across the fan-out.
    sim_cfg = config.simulator_config
    sim_openai_key = api_keys.get("openai")
    simulator_client = None
    if sim_cfg.enabled and sim_openai_key:
        from openai import AsyncOpenAI
        simulator_client = AsyncOpenAI(api_key=sim_openai_key)

    yield SSEEvent(
        event="benchmark:start",
        data={
            "run_id": run_id,
            "total_prompts": len(prompt_indices),
            "total_models": panel_count + candidate_count,
            "panel_models": [mc.id for mc in panel_mcs],
            "candidate_models": [mc.id for mc in candidate_mcs],
            "seeded_facts": seeded_facts,  # {prompt_index: count}
            "oracle_prompts": [pi for pi, t in oracle_by_prompt.items() if t],
            # {prompt_index: gold_code} for every prompt that has one, so the
            # live UI can mark answers against gold instead of the reference.
            "gold_codes": {
                str(pi): code
                for pi in prompt_indices
                if (code := get_gold_code(pi))
            },
            # The achievable ceiling. Where gold is not in the candidate set,
            # no model can be right, so accuracy read without this is
            # measuring retrieval staleness and calling it model quality.
            "gold_retrievable": _current_run.gold_retrievable,
            "max_rounds": MAX_ROUNDS,
            "simulator_enabled": simulator_client is not None,
            "simulator_model": sim_cfg.model if simulator_client else None,
        },
    )

    if gold_mode_effective:
        # Tells the UI console why the panel/consensus/judge phases are absent.
        yield SSEEvent(
            event="benchmark:gold_mode",
            data={
                "enabled": True,
                "reason": (
                    "explicitly requested" if gold_mode
                    else "all prompts have gold codes"
                ),
            },
        )

    if gold_mode_effective:
        # Gold mode runs no reference phase, so a prompt without a gold code
        # has nothing to be scored against. It still runs (the Q&A transcript
        # is often what you wanted) but contributes to no accuracy figure -
        # say which ones rather than quietly averaging over fewer prompts.
        unscored = [pi for pi in prompt_indices if not get_gold_code(pi)]
        if unscored:
            yield SSEEvent(
                event="benchmark:unscored_prompts",
                data={
                    "prompt_indices": unscored,
                    "message": (
                        f"{len(unscored)} selected prompt(s) have no gold code "
                        f"({', '.join('#' + str(pi) for pi in unscored)}). They will run "
                        "but are not scored. Deselect them, or switch scoring to the "
                        "reference model to score everything against a model-built baseline."
                    ),
                },
            )

    if deduped_into_panel:
        # A selected model that is also the reference runs as the reference and
        # never appears as a scored candidate. Say so, rather than letting it
        # vanish from the results table with no explanation.
        yield SSEEvent(
            event="benchmark:model_deduped",
            data={
                "model_ids": deduped_into_panel,
                "message": (
                    f"{', '.join(deduped_into_panel)} is the configured reference model, "
                    "so it runs as the reference and is not scored as a candidate. "
                    "Change Configure > Reference Model to score it head-to-head."
                ),
            },
        )

    # Single event bus: simulator + Q&A loops + task wrappers all push into
    # this. The orchestrator drains it serially in each phase loop, yielding
    # ("live", ...) items as SSE immediately and handling phase-completion
    # items (panel_done/candidate_done/judge_done) inline.
    event_bus: asyncio.Queue = asyncio.Queue()

    def snapshot_fact_store_to_run() -> None:
        """Update _current_run.fact_store so Analysis tab mid-run shows live
        state if the user switches tabs while the run is still going."""
        _current_run.fact_store = {
            str(pi): fact_store.as_dict(pi) for pi in prompt_indices
        }

    # Push any pre-seeded fact commits onto the bus so they stream as
    # simulator:commit SSE events at the start of phase 1. drain_new_commits
    # gives us the list captured during fact_store.seed() above.
    seeded_commits, _seed_idx = fact_store.drain_new_commits(0)
    for c in seeded_commits:
        event_bus.put_nowait(("live", "simulator:commit", c))

    # ── Phase 1: Run all panel models on all prompts in parallel ──
    panel_task_count = len(prompt_indices) * panel_count

    async def run_panel_prompt(mc: ModelConfig, pi: int) -> None:
        try:
            result = await _run_qa_loop(
                get_provider(mc, api_keys), mc, pi, api_keys, opensearch_limit,
                simulator_client=simulator_client, simulator_config=sim_cfg,
                fact_store=fact_store,
                oracle_text=oracle_by_prompt.get(pi),
                event_bus=event_bus,
                is_panel=True,
            )
            await event_bus.put(("panel_done", mc.id, pi, result))
        except Exception as exc:
            await event_bus.put(("panel_done", mc.id, pi, exc))

    # In gold mode panel_mcs is empty: no panel:start event, no panel tasks,
    # and the drain loop below is a no-op (panel_task_count == 0).
    if not gold_mode_effective:
        yield SSEEvent(event="panel:start", data={
            "panel_models": [mc.id for mc in panel_mcs],
            "total_tasks": panel_task_count,
        })

    for mc in panel_mcs:
        for pi in prompt_indices:
            all_tasks.append(asyncio.create_task(run_panel_prompt(mc, pi)))

    panel_done = 0
    # Same idle-connection problem as the fan-out phase: a quiet stretch with
    # no events lets a proxy drop the stream. Behind an ALB (60s default idle
    # timeout) this phase would be the first to die.
    panel_last_event = asyncio.get_running_loop().time()
    panel_last_heartbeat = 0.0
    while panel_done < panel_task_count:
        if check_cancelled():
            _current_run.status = "cancelled"
            _save_run(_current_run)
            yield SSEEvent(event="benchmark:cancelled", data={"phase": "panel", "reason": "user"})
            return
        try:
            item = await asyncio.wait_for(event_bus.get(), timeout=1.0)
            panel_last_event = asyncio.get_running_loop().time()
        except asyncio.TimeoutError:
            now = asyncio.get_running_loop().time()
            stalled_for = now - panel_last_event
            if stalled_for >= FANOUT_STALL_TIMEOUT_S:
                for task in all_tasks:
                    if not task.done():
                        task.cancel()
                _current_run.issues.append({
                    "kind": "error",
                    "source": "panel_stall",
                    "message": (
                        f"{panel_task_count - panel_done} reference task(s) cancelled "
                        f"after {round(stalled_for)}s with no activity"
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                yield SSEEvent(event="benchmark:panel_stalled", data={
                    "outstanding": panel_task_count - panel_done,
                    "stalled_seconds": round(stalled_for),
                })
                break
            if now - panel_last_heartbeat >= 15.0:
                panel_last_heartbeat = now
                yield SSEEvent(event="benchmark:waiting", data={
                    "phase": "panel",
                    "done": panel_done,
                    "total": panel_task_count,
                    "stalled_seconds": round(stalled_for),
                })
            continue
        kind = item[0]
        if kind == "live":
            _, name, data = item
            yield SSEEvent(event=name, data=data)
            continue
        if kind != "panel_done":
            # Defensive: stray item from a prior phase shouldn't reach here,
            # but if it does, drop it rather than deadlock.
            continue

        _, mid, pi, result = item

        if isinstance(result, Exception):
            result = CompletionResult(
                model_id=mid,
                prompt_index=pi,
                response_text="",
                response_type="error",
                error=str(result),
            )

        _current_run.panel_results.append(result)
        completed += 1
        _current_run.progress = completed / total_tasks
        checkpoint_current_run()

        # Surface any task-level errors (reached after retries were exhausted
        # in the provider layer) into the run's issues log + SSE so the UI
        # can display them without having to grep response_text for errors.
        if result.error:
            issue = {
                "kind": "error",
                "source": "reference",
                "model_id": result.model_id,
                "prompt_index": result.prompt_index,
                "message": result.error[:240],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            _current_run.issues.append(issue)
            yield SSEEvent(event="task:failed", data=issue)

        snapshot_fact_store_to_run()

        yield SSEEvent(
            event="panel:complete",
            data={
                "model_id": mid,
                "prompt_index": pi,
                "response_type": result.response_type,
                "total_rounds": result.total_rounds,
                "total_latency_ms": result.total_latency_ms,
                "total_tokens": result.total_input_tokens + result.total_output_tokens,
                "total_cost": result.total_cost,
                "top_code": _extract_top_code(result.response_text),
                "error": result.error,
            },
        )
        panel_done += 1

    # ── Phase 1.5: Compute consensus per prompt + tag each prompt's OTT section ──
    consensus_results: dict[int, CompletionResult] = {}
    panel_agreements: dict[int, float] = {}

    if gold_mode_effective:
        # No consensus in gold mode. Sections are normally derived from the
        # reference's top code; derive them from the gold code instead so the
        # stratified summary views still work.
        for pi in prompt_indices:
            gold = get_gold_code(pi)
            section = section_for_code(gold)
            if section is not None:
                _current_run.prompt_sections[str(pi)] = section
                yield SSEEvent(event="prompt:section", data={
                    "prompt_index": pi,
                    "section_number": section["number"],
                    "section_title": section["title"],
                    "roman": section["roman"],
                    "reference_top_code": gold,
                })
    else:
        for pi in prompt_indices:
            consensus, agreement = compute_consensus(_current_run.panel_results, pi)
            consensus_results[pi] = consensus
            panel_agreements[pi] = agreement
            _current_run.consensus_results.append(consensus)
            # Backward compat: also populate baseline_results
            _current_run.baseline_results.append(consensus)

            # Tag this prompt with its OTT section derived from the reference's
            # top commodity code. Stratified summary views use this.
            ref_top = _extract_top_code(consensus.response_text)
            section = section_for_code(ref_top)
            if section is not None:
                _current_run.prompt_sections[str(pi)] = section
                yield SSEEvent(event="prompt:section", data={
                    "prompt_index": pi,
                    "section_number": section["number"],
                    "section_title": section["title"],
                    "roman": section["roman"],
                    "reference_top_code": ref_top,
                })

        yield SSEEvent(event="consensus:complete", data={
            "prompt_count": len(prompt_indices),
            "avg_panel_agreement": round(
                sum(panel_agreements.values()) / len(panel_agreements), 4
            ) if panel_agreements else 0.0,
        })

    # ── Setup judge infrastructure (runs in parallel with candidates) ──
    judge_cfg = config.judge_config
    openai_key = api_keys.get("openai")
    # Gold mode forces the judge off: gold scoring is deterministic and the
    # judge's dimensions add no correctness information there.
    judge_enabled = bool(openai_key and judge_cfg.enabled) and not gold_mode_effective
    judge_client = None
    judge_count = 0
    judge_tasks: dict[int, asyncio.Task] = {}
    pending_judges: set[int] = set()

    if judge_enabled:
        from openai import AsyncOpenAI

        judge_client = AsyncOpenAI(api_key=openai_key)

        async def run_judge(eval_idx: int, ev: EvaluationResult) -> None:
            try:
                consensus = consensus_results.get(ev.prompt_index)
                target = None
                if ev.model_id == "consensus":
                    target = consensus
                else:
                    for r in _current_run.model_results:
                        if r.model_id == ev.model_id and r.prompt_index == ev.prompt_index:
                            target = r
                            break
                if not consensus or not target:
                    await event_bus.put(("judge_done", eval_idx, {}))
                    return
                query = get_raw_query(ev.prompt_index)
                # Pass the per-prompt fact store snapshot so the judge can score
                # fact_consistency and understand the apples-to-apples property.
                facts = fact_store.as_dict(ev.prompt_index)
                scores = await asyncio.wait_for(
                    judge_response(
                        judge_client, query, consensus.response_text, target.response_text, judge_cfg,
                        baseline_rounds=consensus.total_rounds, target_rounds=target.total_rounds,
                        is_baseline=(ev.model_id == "consensus"),
                        facts=facts,
                    ),
                    timeout=JUDGE_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                scores = {
                    "fact_consistency": None,
                    "question_quality": None,
                    "reasoning": f"Judge error: {str(exc)[:200]}",
                    "cost": 0.0,
                    "latency_ms": 0.0,
                    "error": True,
                }
            await event_bus.put(("judge_done", eval_idx, scores))

    # ── Phase 2a: Baseline judge evals fire immediately ──
    # These start running now, overlapping with candidate model calls below.
    for pi in prompt_indices:
        consensus = consensus_results.get(pi)
        if consensus is None or consensus.error:
            continue
        # The reference's self-evaluation: it matches itself on every deterministic
        # code-agreement dimension by construction. Gold-truth scoring is NOT
        # by-construction - the reference may disagree with gold, which is
        # the single most valuable signal when a gold set is available.
        gold = get_gold_code(pi)
        ref_codes = _extract_codes(consensus.response_text)
        ref_gold = compute_gold_metrics(ref_codes, gold)
        baseline_eval = EvaluationResult(
            model_id="consensus",
            prompt_index=pi,
            cosine_similarity=1.0,
            code_match_score=1.0,
            top1_match=True,
            top3_hit=True,
            top5_overlap=1.0,
            mean_reciprocal_rank=1.0,
            heading_match=True,
            chapter_match=True,
            hierarchical_score=1.0,
            schema_valid=1.0 if consensus.response_type == "answers" else 0.0,
            total_questions=sum(len(r.questions_asked) for r in consensus.rounds),
            new_slots_set=sum(
                1 for r in consensus.rounds
                for t in (r.simulator_trace or [])
                if isinstance(t, dict) and not t.get("consistent_with_prior", False)
            ),
            question_efficiency=1.0,  # reference sets everything it asks about
            rounds_efficiency=max(
                0.0, 1.0 - (consensus.total_rounds - 1) / 4.0,
            ),
            gold_code=str(gold) if gold else None,
            gold_top1_match=ref_gold["gold_top1_match"],
            gold_heading_match=ref_gold["gold_heading_match"],
            gold_chapter_match=ref_gold["gold_chapter_match"],
            gold_hierarchical_score=ref_gold["gold_hierarchical_score"],
            delta_score=0.0,
            total_latency_ms=round(consensus.total_latency_ms, 1),
            baseline_total_latency_ms=round(consensus.total_latency_ms, 1),
            speed_factor=1.0,
            total_cost=round(consensus.total_cost, 6),
            baseline_total_cost=round(consensus.total_cost, 6),
            total_rounds=consensus.total_rounds,
            baseline_total_rounds=consensus.total_rounds,
        )
        baseline_eval.panel_agreement = panel_agreements.get(pi)
        _current_run.evaluations.append(baseline_eval)
        # The reference IS the answer key by construction - do NOT judge it.
        # Circular self-scoring was producing misleadingly low numbers (2.33)
        # on the consensus row whenever the reference had a weaker prompt.
        # In future panel mode, individual panel members will still be judged
        # vs consensus for intra-panel disagreement analysis.

    # ── Phase 2b: Fan-out candidates + evaluate + judge as each completes ──
    fan_out_count = 0

    async def run_model_prompt(mc: ModelConfig, pi: int) -> None:
        try:
            result = await _run_qa_loop(
                get_provider(mc, api_keys), mc, pi, api_keys, opensearch_limit,
                simulator_client=simulator_client, simulator_config=sim_cfg,
                fact_store=fact_store,
                oracle_text=oracle_by_prompt.get(pi),
                event_bus=event_bus,
                is_panel=False,
            )
            await event_bus.put(("candidate_done", mc.id, pi, result))
        except Exception as exc:
            await event_bus.put(("candidate_done", mc.id, pi, exc))

    # Models run SEQUENTIALLY within a prompt, prompts run concurrently.
    #
    # The fact store is first-writer-wins: the first model to ask about a
    # concept fixes the answer for every model after it. Firing all
    # (model, prompt) pairs concurrently meant the winner of that race was
    # decided by asyncio scheduling, so two identical runs could commit
    # different facts and hand every model a different product. Measured on
    # two back-to-back runs: 10 of 20 prompts ended with a different set of
    # slots, and 4 shared slots got different answers - every one of them
    # first-written by a different model. On prompt 43 the committed carbon
    # content differed, which is the classification for that heading.
    #
    # Fixing the order makes the same model always write first, so a repeat
    # run sees the same product. Prompts stay parallel, so the wall-clock cost
    # is bounded by models-per-prompt, not by the full fan-out.
    async def run_prompt_all_models(pi: int) -> None:
        for mc in candidate_mcs:
            await run_model_prompt(mc, pi)

    for pi in prompt_indices:
        all_tasks.append(asyncio.create_task(run_prompt_all_models(pi)))
    fan_out_count = len(prompt_indices) * len(candidate_mcs)

    yield SSEEvent(event="fanout:start", data={"total_tasks": fan_out_count})

    candidate_done_count = 0
    # A single hung task used to stall the entire run: the loop below waits on
    # the event bus forever, emitting nothing, so the run never finalises and
    # the idle connection is eventually dropped by an intermediary. Three
    # 20x3 runs died at 59/60 that way, each on a different task. Bound the
    # wait, and emit a heartbeat while waiting so the stream stays alive and
    # the UI can say what is outstanding.
    fanout_last_event = asyncio.get_running_loop().time()
    fanout_last_heartbeat = 0.0
    while candidate_done_count < fan_out_count:
        if check_cancelled():
            _current_run.status = "cancelled"
            _save_run(_current_run)
            yield SSEEvent(event="benchmark:cancelled", data={"phase": "fanout", "reason": "user"})
            return
        try:
            item = await asyncio.wait_for(event_bus.get(), timeout=1.0)
            fanout_last_event = asyncio.get_running_loop().time()
        except asyncio.TimeoutError:
            now = asyncio.get_running_loop().time()
            stalled_for = now - fanout_last_event
            if stalled_for >= FANOUT_STALL_TIMEOUT_S:
                outstanding = fan_out_count - candidate_done_count
                for task in all_tasks:
                    if not task.done():
                        task.cancel()
                yield SSEEvent(event="benchmark:fanout_stalled", data={
                    "outstanding": outstanding,
                    "stalled_seconds": round(stalled_for),
                    "message": (
                        f"{outstanding} task(s) produced no events for "
                        f"{round(stalled_for)}s and were cancelled. The run is "
                        "finalised with the completions it already has."
                    ),
                })
                _current_run.issues.append({
                    "kind": "error",
                    "source": "fanout_stall",
                    "message": (
                        f"{outstanding} task(s) cancelled after {round(stalled_for)}s "
                        "with no activity; run finalised with the completions it had"
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                break
            if now - fanout_last_heartbeat >= 15.0:
                fanout_last_heartbeat = now
                yield SSEEvent(event="benchmark:waiting", data={
                    "phase": "fanout",
                    "done": candidate_done_count,
                    "total": fan_out_count,
                    "stalled_seconds": round(stalled_for),
                })
            continue
        kind = item[0]
        if kind == "live":
            _, name, data = item
            yield SSEEvent(event=name, data=data)
            continue
        if kind != "candidate_done":
            # Defensive: ignore stray items from other phases.
            continue

        _, mid, pi, result = item

        if isinstance(result, Exception):
            result = CompletionResult(
                model_id=mid,
                prompt_index=pi,
                response_text="",
                response_type="error",
                error=str(result),
            )

        _current_run.model_results.append(result)
        completed += 1
        _current_run.progress = completed / total_tasks
        checkpoint_current_run()

        # Evaluate against consensus and fire judge immediately. Pass the
        # prompt's gold_code (if any) so candidates get scored against the
        # known-correct answer in addition to the reference.
        consensus = consensus_results.get(result.prompt_index)
        if gold_mode_effective and not result.error:
            # Gold mode: no consensus exists. Score against gold only;
            # reference-agreement fields stay None.
            evaluation = _evaluate_gold_only(
                result, get_gold_code(result.prompt_index),
            )
            _current_run.evaluations.append(evaluation)
        elif consensus and not result.error:
            evaluation = evaluate_pair(
                consensus, result, gold_code=get_gold_code(result.prompt_index),
            )
            evaluation.panel_agreement = panel_agreements.get(result.prompt_index)
            _current_run.evaluations.append(evaluation)
            if judge_enabled:
                eval_idx = len(_current_run.evaluations) - 1
                task = asyncio.create_task(run_judge(eval_idx, evaluation))
                all_tasks.append(task)
                judge_tasks[eval_idx] = task
                pending_judges.add(eval_idx)
                judge_count += 1

        snapshot_fact_store_to_run()

        yield SSEEvent(
            event="model:complete",
            data={
                "model_id": mid,
                "prompt_index": pi,
                "response_type": result.response_type,
                "total_rounds": result.total_rounds,
                "total_latency_ms": result.total_latency_ms,
                "total_tokens": result.total_input_tokens + result.total_output_tokens,
                "total_cost": result.total_cost,
                "top_code": _extract_top_code(result.response_text),
                "error": result.error,
            },
        )
        candidate_done_count += 1

    # ── Phase 3: Drain judge results (many may already be done) ──
    if judge_enabled and judge_count > 0:
        yield SSEEvent(event="judge:start", data={"total": judge_count, "model": judge_cfg.model})

        judge_done = 0
        judge_deadline = asyncio.get_running_loop().time() + JUDGE_DRAIN_TIMEOUT_S
        last_wait_event = 0.0
        while judge_done < judge_count:
            if check_cancelled():
                _current_run.status = "cancelled"
                _save_run(_current_run)
                yield SSEEvent(event="benchmark:cancelled", data={"phase": "judge", "reason": "user"})
                return
            now = asyncio.get_running_loop().time()
            if now >= judge_deadline:
                for task in judge_tasks.values():
                    if not task.done():
                        task.cancel()
                for eval_idx in sorted(pending_judges):
                    ev = _current_run.evaluations[eval_idx]
                    ev.judge_error = True
                    ev.judge_reasoning = (
                        f"Judge timed out after {JUDGE_DRAIN_TIMEOUT_S}s drain window; "
                        "deterministic metrics are still available."
                    )
                    issue = {
                        "kind": "error",
                        "source": "judge",
                        "model_id": ev.model_id,
                        "prompt_index": ev.prompt_index,
                        "message": ev.judge_reasoning,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    _current_run.issues.append(issue)
                    judge_done += 1
                    yield SSEEvent(event="judge:complete", data={
                        "done": judge_done,
                        "total": judge_count,
                        "model_id": ev.model_id,
                        "timed_out": True,
                    })
                pending_judges.clear()
                break
            try:
                item = await asyncio.wait_for(event_bus.get(), timeout=1.0)
            except asyncio.TimeoutError:
                now = asyncio.get_running_loop().time()
                if now - last_wait_event >= 15.0:
                    last_wait_event = now
                    yield SSEEvent(event="judge:waiting", data={
                        "done": judge_done,
                        "total": judge_count,
                        "pending": judge_count - judge_done,
                        "seconds_remaining": max(0, round(judge_deadline - now)),
                    })
                continue
            kind = item[0]
            if kind == "live":
                _, name, data = item
                yield SSEEvent(event=name, data=data)
                continue
            if kind != "judge_done":
                continue
            _, eval_idx, scores = item
            pending_judges.discard(eval_idx)
            if scores:
                ev = _current_run.evaluations[eval_idx]
                # Judge now scores TWO dimensions: fact_consistency and
                # question_quality. Legacy judge fields (classification_accuracy,
                # structured_output, overall) are left as None - their
                # deterministic equivalents in EvaluationResult replace them:
                #   classification_accuracy -> top1_match/heading_match/chapter_match/top5_overlap
                #   structured_output       -> schema_valid
                #   overall                 -> composite computed from scoring_weights
                ev.judge_fact_consistency = scores.get("fact_consistency")
                ev.judge_question_quality = scores.get("question_quality")
                ev.judge_reasoning = scores.get("reasoning")
                ev.judge_cost = scores.get("cost", 0.0)
                ev.judge_error = bool(scores.get("error", False))
                # delta_score is no longer derived from judge; it's computed
                # from the full composite in the frontend verdict math.
            judge_done += 1
            yield SSEEvent(event="judge:complete", data={
                "done": judge_done,
                "total": judge_count,
                "model_id": _current_run.evaluations[eval_idx].model_id if scores else "",
            })

    # ── Phase 4: Summaries ──
    _current_run.summaries = compute_summaries(_current_run, model_map)

    # Persist the per-prompt fact store snapshot so the UI can render it.
    _current_run.fact_store = {
        str(pi): fact_store.as_dict(pi) for pi in prompt_indices
    }

    _current_run.status = "complete"
    _current_run.progress = 1.0
    _save_run(_current_run)

    yield SSEEvent(
        event="benchmark:complete",
        data={"run_id": run_id, "summary_count": len(_current_run.summaries)},
    )
