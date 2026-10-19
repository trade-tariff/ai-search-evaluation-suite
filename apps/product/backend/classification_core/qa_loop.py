"""Q&A loop orchestrator + oracle-backed trader simulator.

Lifts the conceptual core from ai-fan-out/backend/simulator.py + fact_store.py
without dragging in the distributed-locking and multi-model machinery the
benchmark sweep needs. The classification workflow is single-session, single-user; we
just need:

  1. Run classify_step. If LLM mode == "answers" -> done.
  2. If mode == "questions" -> pick an answer (via oracle simulator OR human),
     commit the slot to the per-session fact store, append to qa_history,
     loop back to step 1.
  3. Stop on convergence (top-1 stable + confident) or MAX_ROUNDS.

Two ways to answer:
  - **Oracle mode**: an authoritative product description (e.g. the ATAR
    ruling body) feeds an LLM call that picks the best option. Used by
    Exp 6 (oracle Q&A upper bound) and end-to-end evals.
  - **Human mode**: the API caller supplies the answer string. Used by the
    actual classification workflow UI.

Per-session facts ensure consistency: if a prior question committed "material
= rubber", the next question about material should not contradict it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

from openai import AsyncOpenAI

from . import classification, local_db
from .provider_guard import openai_allowed
from .trader_simulator import (
    CommittedFact,
    SessionFacts,
    SIMULATOR_MODEL,
    SIMULATOR_MAX_RETRIES,
    _match_to_options,
    simulate_trader_answer as _simulate_trader_answer,
)

MAX_ROUNDS_DEFAULT = 5  # User-confirmed acceptable budget (was 3 in original Exp 6)


def _flat_commodity_code(code: str) -> str:
    digits = re.sub(r"\D", "", str(code or ""))
    return digits.ljust(10, "0")[:10] if digits else ""


def _normalise_key(value: str) -> str:
    out = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower())
    out = re.sub(r"_+", "_", out).strip("_")
    return out[:120]


def _normalise_value(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower())
    out = re.sub(r"_+", "_", out).strip("_")
    return out[:240]


def _role_for_fact_key(key: str) -> str:
    key_l = key.lower()
    if "material" in key_l or "composition" in key_l:
        return "material_composition"
    if "use" in key_l or "function" in key_l or "purpose" in key_l:
        return "function_use"
    if "origin" in key_l or "country" in key_l or "region" in key_l:
        return "origin_or_region"
    if any(token in key_l for token in ("pack", "container", "size", "form", "presentation")):
        return "form_presentation"
    return "product_identity"


def _fact_key_value(fact: dict) -> tuple[str, str, float, str]:
    key = _normalise_key(str(fact.get("key") or fact.get("slot") or ""))
    value = _normalise_value(str(fact.get("value") or fact.get("answer") or ""))
    confidence_raw = fact.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 0.95
    except Exception:
        confidence = 0.95
    confidence = max(0.0, min(1.0, confidence))
    evidence = str(
        fact.get("source_span")
        or fact.get("source_question")
        or fact.get("question")
        or ""
    ).strip()
    return key, value, confidence, evidence


def persist_session_facts_to_kg(
    *,
    commodity_code: str,
    facts: list[dict],
    source: str = "qna_session",
    provenance: Optional[dict] = None,
    use_scopes: Optional[list[str]] = None,
    authority_tier: int | None = None,
) -> dict:
    """Persist committed/session-extracted facts into kg.commodity_facets.

    These are not canonical legal/tariff facts. They are user/session-derived
    CC facts with explicit source/provenance so downstream retrieval, Q&A, and
    audits can distinguish them from seeded KG evidence.
    """
    code = _flat_commodity_code(commodity_code)
    raw_facts = [f for f in (facts or []) if isinstance(f, dict)]
    summary = {
        "available": True,
        "commodity_code": code,
        "source": source,
        "attempted": len(raw_facts),
        "persisted": 0,
        "skipped": 0,
        "ids": [],
    }
    if not code or not raw_facts:
        summary["skipped"] = len(raw_facts)
        if not code:
            summary["error"] = "missing commodity_code"
        return summary

    scopes = use_scopes or ["classification", "qa", "audit"]
    kg_schema = local_db.KG_SCHEMA
    provenance_in = provenance or {}
    try:
        tier = int(
            authority_tier
            if authority_tier is not None
            else provenance_in.get("authority_tier")
            or provenance_in.get("source_authority_tier")
            or 8
        )
    except Exception:
        tier = 8
    tier = max(1, min(8, tier))
    base_provenance = {
        "kind": "session_fact",
        "source": source,
        "capture_method": "qna_session",
        "authority_tier": tier,
        **provenance_in,
    }
    source = str(source or "qna_session")[:180]
    try:
        with local_db._conn() as conn, conn.cursor() as cur:
            for idx, fact in enumerate(raw_facts):
                key, value, confidence, evidence = _fact_key_value(fact)
                if not key or not value:
                    summary["skipped"] += 1
                    continue
                label = key.replace("_", " ").strip().title() or key
                cur.execute(
                    f"""
                    INSERT INTO {kg_schema}.facet_definitions
                      (key, label, short_label, value_set, rank)
                    VALUES (%s, %s, %s, '[]'::jsonb, 9999)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    (key, label, label[:80]),
                )
                fact_provenance = {
                    **base_provenance,
                    "fact_index": idx,
                    "round_number": fact.get("round_number"),
                    "slot": fact.get("slot") or fact.get("key"),
                    "raw_value": fact.get("answer") or fact.get("value"),
                    "source_question": fact.get("source_question") or fact.get("question"),
                    "source_span": fact.get("source_span"),
                }
                role = _role_for_fact_key(key)
                cur.execute(
                    f"""
                    INSERT INTO {kg_schema}.commodity_facets
                      (commodity_code, facet_key, facet_value, source, confidence,
                       evidence, authority_tier, provenance, use_scopes,
                       evidence_roles, embedding_stale)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::text[], %s::text[], true)
                    ON CONFLICT (commodity_code, facet_key, facet_value, source)
                    DO UPDATE SET
                      confidence = GREATEST({kg_schema}.commodity_facets.confidence, EXCLUDED.confidence),
                      evidence = COALESCE(EXCLUDED.evidence, {kg_schema}.commodity_facets.evidence),
                      provenance = COALESCE({kg_schema}.commodity_facets.provenance, '{{}}'::jsonb) || EXCLUDED.provenance,
                      use_scopes = EXCLUDED.use_scopes,
                      evidence_roles = EXCLUDED.evidence_roles,
                      updated_at = now(),
                      embedding_stale = ({kg_schema}.commodity_facets.embedding IS NULL)
                    RETURNING id
                    """,
                    (
                        code,
                        key,
                        value,
                        source,
                        confidence,
                        evidence or None,
                        tier,
                        json.dumps(fact_provenance, default=str),
                        scopes,
                        [role],
                    ),
                )
                row = cur.fetchone()
                if row and row.get("id") is not None:
                    summary["ids"].append(int(row["id"]))
                summary["persisted"] += 1
            conn.commit()
    except Exception as exc:
        summary["available"] = False
        summary["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return summary


def session_facts_persist_skipped(facts) -> dict:
    """Summary to record when a runner deliberately does NOT persist facts.

    Evaluation runners keep their oracle/session facts out of the shared KG
    classification/qa scopes by default, because those scopes are what their
    own later sessions and readers retrieve from. The shape mirrors
    persist_session_facts_to_kg's return so result readers can treat both
    uniformly.
    """
    n = len(facts or [])
    return {"opt_in": False, "attempted": n, "persisted": 0, "skipped": n, "ids": []}


# ---------- legacy simulator entrypoint ----------

async def simulate_trader_answer(
    client: AsyncOpenAI,
    session: SessionFacts,
    raw_query: str,
    question: str,
    options: list[str],
    round_number: int,
    oracle_text: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Keep the legacy research callers' mutable default model setting."""
    return await _simulate_trader_answer(
        client=client, session=session, raw_query=raw_query, question=question,
        options=options, round_number=round_number, oracle_text=oracle_text,
        model=model or SIMULATOR_MODEL,
    )


# ---------- orchestrator ----------

async def run_qa_session(
    query: str,
    max_rounds: int = MAX_ROUNDS_DEFAULT,
    oracle_text: Optional[str] = None,
    config: Optional[dict] = None,
    human_answers: Optional[list[str]] = None,
) -> dict:
    """Run the full Q&A loop until an answer commit, max rounds, or no-candidates.

    Args:
        query: the trader's raw query (e.g. "shoes")
        max_rounds: cap on Q&A rounds before forcing classify to commit
        oracle_text: if set, answers come from an LLM call with this as ground truth.
            (e.g. the ATAR ruling body for eval). If None and human_answers is None,
            we fall back to picking options[0] for each question (cheap dumb mode).
        config: classification config dict passed through to classify_step
        human_answers: list of pre-supplied answer strings; iff set, one per round
            (no LLM simulator call). For dev/testing without paying for sim calls.

    Returns: {
        "final_mode": str,        # "answers" | "questions" (timeout) | "no_candidates" | "error"
        "rounds": [round_dict, ...],
        "qa_history": [{question, answer}, ...],
        "facts": [{slot, answer, source_question, round_number}, ...],
        "final_answers": [...] | None,   # if final_mode == "answers"
        "final_question": {...} | None,  # if final_mode == "questions" (we hit max_rounds)
        "candidates_final_round": [...],
        "total_classify_calls": int,
        "total_simulator_calls": int,
    }
    """
    # Strategy dispatch: 'eliminate' fixes the candidate set at round 1 and only
    # rules out definitively-excluded candidates (never re-retrieves). Default
    # 'converge' is the original re-retrieve-and-recommit flow below.
    strategy = (config or {}).get("strategy", "converge")
    if strategy == "eliminate":
        return await _run_eliminate_session(
            query=query, max_rounds=max_rounds, oracle_text=oracle_text,
            config=config, human_answers=human_answers,
        )

    api_key = os.environ.get("OPENAI_API_KEY")
    sim_client = AsyncOpenAI(api_key=api_key) if (openai_allowed() and api_key and oracle_text) else None

    session = SessionFacts()
    rounds: list[dict] = []
    total_classify = 0
    total_simulator = 0

    last_turn: dict = {}
    for round_num in range(1, max_rounds + 1):
        qa_history = session.as_qa_history()
        # classify_step is sync; run in a threadpool to keep this async-friendly.
        loop = asyncio.get_running_loop()
        turn = await loop.run_in_executor(
            None, lambda: classification.classify_step(query, qa_history, config),
        )
        total_classify += 1
        last_turn = turn
        mode = turn.get("mode")
        round_record: dict = {
            "round_number": round_num,
            "mode": mode,
            "candidates_top5": [
                {"code": c["commodity_code"], "description": (c.get("description") or "")[:80]}
                for c in turn.get("candidates", [])[:5]
            ],
        }

        if mode == "answers":
            round_record["answers"] = turn.get("answers", [])
            rounds.append(round_record)
            return {
                "final_mode": "answers",
                "rounds": rounds,
                "qa_history": session.as_qa_history(),
                "facts": [f.__dict__ for f in session.snapshot().values()],
                "final_answers": turn.get("answers", []),
                "final_question": None,
                "candidates_final_round": turn.get("candidates", []),
                "total_classify_calls": total_classify,
                "total_simulator_calls": total_simulator,
            }

        if mode != "questions":
            # no_candidates or error - stop the loop
            rounds.append(round_record)
            return {
                "final_mode": mode,
                "rounds": rounds,
                "qa_history": session.as_qa_history(),
                "facts": [f.__dict__ for f in session.snapshot().values()],
                "final_answers": None,
                "final_question": turn.get("question"),
                "candidates_final_round": turn.get("candidates", []),
                "total_classify_calls": total_classify,
                "total_simulator_calls": total_simulator,
            }

        question_obj = turn.get("question") or {}
        question_text = question_obj.get("question") or ""
        options = question_obj.get("options") or []
        round_record["question"] = question_text
        round_record["options"] = options

        # Pick the answer
        if human_answers is not None and len(human_answers) >= round_num:
            chosen = human_answers[round_num - 1]
            matched = _match_to_options(chosen, options) or chosen
            session.record(
                slot=f"human_round_{round_num}",
                answer=matched, source_question=question_text, round_number=round_num,
            )
            round_record["chosen"] = matched
            round_record["answer_source"] = "human"
        elif sim_client is not None and options:
            sim_result = await simulate_trader_answer(
                client=sim_client, session=session,
                raw_query=query, question=question_text, options=options,
                round_number=round_num, oracle_text=oracle_text,
            )
            total_simulator += 1
            round_record["chosen"] = sim_result.get("chosen")
            round_record["choice_index"] = sim_result.get("choice_index")
            round_record["slot"] = sim_result["slot"]
            round_record["reasoning"] = sim_result.get("reasoning")
            round_record["answer_source"] = "simulator"
            round_record["simulator_attempts"] = sim_result.get("attempts")
            if sim_result.get("simulator_failed"):
                # Bail out cleanly - no committed fact, no garbage in qa_history.
                round_record["simulator_failed"] = True
                round_record["simulator_error"] = sim_result.get("last_error")
                rounds.append(round_record)
                return {
                    "final_mode": "simulator_failed",
                    "rounds": rounds,
                    "qa_history": session.as_qa_history(),
                    "facts": [f.__dict__ for f in session.snapshot().values()],
                    "final_answers": None,
                    "final_question": turn.get("question"),
                    "candidates_final_round": turn.get("candidates", []),
                    "total_classify_calls": total_classify,
                    "total_simulator_calls": total_simulator,
                }
        else:
            # Dumb fallback: pick options[0]
            chosen = options[0] if options else "Yes"
            session.record(
                slot=f"fallback_round_{round_num}",
                answer=chosen, source_question=question_text, round_number=round_num,
            )
            round_record["chosen"] = chosen
            round_record["answer_source"] = "fallback_first_option"

        rounds.append(round_record)

    # Hit max_rounds without convergence
    return {
        "final_mode": "questions",  # still asking
        "rounds": rounds,
        "qa_history": session.as_qa_history(),
        "facts": [f.__dict__ for f in session.snapshot().values()],
        "final_answers": None,
        "final_question": last_turn.get("question"),
        "candidates_final_round": last_turn.get("candidates", []),
        "total_classify_calls": total_classify,
        "total_simulator_calls": total_simulator,
    }


# ---------- ELIMINATE strategy orchestrator ----------

async def _run_eliminate_session(
    query: str,
    max_rounds: int,
    oracle_text: Optional[str],
    config: Optional[dict],
    human_answers: Optional[list[str]],
) -> dict:
    """Eliminate flow: retrieve ONCE at round 1, freeze the candidate set, then
    each round rule out definitively-excluded candidates and present survivors
    ranked. Never re-retrieves. Returns the SAME result shape as run_qa_session's
    converge path (plus strategy markers) so the harness is strategy-agnostic.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    sim_client = AsyncOpenAI(api_key=api_key) if (openai_allowed() and api_key and oracle_text) else None

    session = SessionFacts()
    rounds: list[dict] = []
    total_classify = 0
    total_simulator = 0
    loop = asyncio.get_running_loop()

    # ---- Round-1 retrieval: freeze the candidate set for the whole session ----
    candidate_limit = int(((config or {}).get("retrieval") or {}).get("limit", 40))
    fixed_raw, _fixed_enriched = await loop.run_in_executor(
        None,
        lambda: classification.initial_candidates_for_eliminate(query, config, candidate_limit),
    )
    if not fixed_raw:
        return {
            "final_mode": "no_candidates", "strategy": "eliminate", "rounds": [],
            "qa_history": [], "facts": [], "final_answers": None, "final_question": None,
            "candidates_final_round": [], "frozen_candidate_count": 0,
            "survivor_count": 0, "survivors_final": [],
            "total_classify_calls": 0, "total_simulator_calls": 0,
        }
    frozen_count = len(fixed_raw)

    last_turn: dict = {}
    for round_num in range(1, max_rounds + 1):
        qa_history = session.as_qa_history()
        turn = await loop.run_in_executor(
            None,
            lambda: classification.eliminate_step(query, qa_history, fixed_raw, config),
        )
        total_classify += 1
        last_turn = turn
        mode = turn.get("mode")
        survivors_all = turn.get("survivors_all") or turn.get("answers") or []
        round_record: dict = {
            "round_number": round_num,
            "mode": mode,
            "survivor_count": len(survivors_all),
            "ruled_out_count": len((turn.get("eliminate_trace") or {}).get("ruled_out", [])),
            "candidates_top5": [
                {"code": c["commodity_code"], "description": (c.get("description") or "")[:80]}
                for c in turn.get("answers", [])[:5]
            ],
        }

        if mode == "answers":
            round_record["answers"] = turn.get("answers", [])
            rounds.append(round_record)
            return {
                "final_mode": "answers", "strategy": "eliminate", "rounds": rounds,
                "qa_history": session.as_qa_history(),
                "facts": [f.__dict__ for f in session.snapshot().values()],
                "final_answers": survivors_all,
                "final_question": None,
                "candidates_final_round": turn.get("candidates", []),
                "frozen_candidate_count": frozen_count,
                "survivor_count": len(survivors_all),
                "survivors_final": survivors_all,
                "total_classify_calls": total_classify,
                "total_simulator_calls": total_simulator,
            }

        if mode != "questions":
            rounds.append(round_record)
            return {
                "final_mode": mode, "strategy": "eliminate", "rounds": rounds,
                "qa_history": session.as_qa_history(),
                "facts": [f.__dict__ for f in session.snapshot().values()],
                "final_answers": survivors_all or None,
                "final_question": turn.get("question"),
                "candidates_final_round": turn.get("candidates", []),
                "frozen_candidate_count": frozen_count,
                "survivor_count": len(survivors_all),
                "survivors_final": survivors_all,
                "total_classify_calls": total_classify,
                "total_simulator_calls": total_simulator,
            }

        # mode == "questions": pick an answer, commit, loop. Identical answer
        # sourcing to the converge path (human / oracle-simulator / fallback).
        question_obj = turn.get("question") or {}
        question_text = question_obj.get("question") or ""
        options = question_obj.get("options") or []
        round_record["question"] = question_text
        round_record["options"] = options

        if human_answers is not None and len(human_answers) >= round_num:
            chosen = human_answers[round_num - 1]
            matched = _match_to_options(chosen, options) or chosen
            session.record(slot=f"human_round_{round_num}", answer=matched,
                           source_question=question_text, round_number=round_num)
            round_record["chosen"] = matched
            round_record["answer_source"] = "human"
        elif sim_client is not None and options:
            sim_result = await simulate_trader_answer(
                client=sim_client, session=session, raw_query=query,
                question=question_text, options=options,
                round_number=round_num, oracle_text=oracle_text,
            )
            total_simulator += 1
            round_record["chosen"] = sim_result.get("chosen")
            round_record["choice_index"] = sim_result.get("choice_index")
            round_record["slot"] = sim_result["slot"]
            round_record["reasoning"] = sim_result.get("reasoning")
            round_record["answer_source"] = "simulator"
            round_record["simulator_attempts"] = sim_result.get("attempts")
            if sim_result.get("simulator_failed"):
                round_record["simulator_failed"] = True
                round_record["simulator_error"] = sim_result.get("last_error")
                rounds.append(round_record)
                return {
                    "final_mode": "simulator_failed", "strategy": "eliminate", "rounds": rounds,
                    "qa_history": session.as_qa_history(),
                    "facts": [f.__dict__ for f in session.snapshot().values()],
                    "final_answers": survivors_all or None,
                    "final_question": turn.get("question"),
                    "candidates_final_round": turn.get("candidates", []),
                    "frozen_candidate_count": frozen_count,
                    "survivor_count": len(survivors_all),
                    "survivors_final": survivors_all,
                    "total_classify_calls": total_classify,
                    "total_simulator_calls": total_simulator,
                }
        else:
            chosen = options[0] if options else "Yes"
            session.record(slot=f"fallback_round_{round_num}", answer=chosen,
                           source_question=question_text, round_number=round_num)
            round_record["chosen"] = chosen
            round_record["answer_source"] = "fallback_first_option"

        rounds.append(round_record)

    # Hit max_rounds still asking -> commit the last surviving set as the answer.
    survivors_all = last_turn.get("survivors_all") or last_turn.get("answers") or []
    return {
        "final_mode": "answers" if survivors_all else "questions",
        "strategy": "eliminate", "rounds": rounds,
        "qa_history": session.as_qa_history(),
        "facts": [f.__dict__ for f in session.snapshot().values()],
        "final_answers": survivors_all or None,
        "final_question": last_turn.get("question"),
        "candidates_final_round": last_turn.get("candidates", []),
        "frozen_candidate_count": frozen_count,
        "survivor_count": len(survivors_all),
        "survivors_final": survivors_all,
        "total_classify_calls": total_classify,
        "total_simulator_calls": total_simulator,
    }
