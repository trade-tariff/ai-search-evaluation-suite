"""End-to-end retrieval + hydrated Q&A matrix.

Evaluates a fixed set of ATAR source pairs across persona inputs:
  input query -> retrieval shortlist -> hydrated fact-sheet Q&A -> filtered result list.

Question modes:
  - facet_rules: deterministic question/options from hydrated fact-sheet signals
  - facet_rules_llm_wording: same deterministic options, LLM rewrites wording only
  - llm_generated: LLM proposes question/options, symbolic mapping controls state

Answerers:
  - gold_option: no-spend oracle upper bound; picks the option whose code map contains gold
  - llm_trader: paid trader emulator using the ATAR body as oracle
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .provider_guard import openai_allowed
from . import qa_loop

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
KG_SCHEMA = os.environ.get("TARIFF_DB_KG_SCHEMA", "kg")
DEFAULT_PERSONAS = [
    "naive_vague",
    "naive_branded",
    "naive_specific",
    "emu_generic",
    "emu_ordinary",
    "emu_specific",
    "original",
]
QUESTION_MODES = ["facet_rules", "facet_rules_llm_wording", "llm_generated"]


def _flat(code: str | None) -> str:
    digits = re.sub(r"\D", "", code or "")
    return digits.ljust(10, "0")[:10] if digits else ""


def _rank_of(codes: list[str], expected: str) -> int | None:
    exp = _flat(expected)
    for idx, code in enumerate(codes, start=1):
        if _flat(code) == exp:
            return idx
    return None


def _candidate_code(row: dict) -> str:
    return str(row.get("commodity_code") or row.get("code") or "")


def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {KG_SCHEMA}.e2e_eval_runs (
              id bigserial PRIMARY KEY,
              run_label text NOT NULL,
              retrieval_run_label text NOT NULL,
              question_mode text NOT NULL,
              answerer text NOT NULL,
              question_model text,
              simulator_model text,
              pair_limit integer NOT NULL,
              persona_count integer NOT NULL,
              input_count integer NOT NULL DEFAULT 0,
              retrieval_limit integer NOT NULL,
              hydrate_limit integer NOT NULL,
              max_rounds integer NOT NULL,
              allow_spend boolean NOT NULL DEFAULT false,
              config_json jsonb NOT NULL DEFAULT '{{}}'::jsonb,
              started_at timestamptz NOT NULL DEFAULT now(),
              finished_at timestamptz,
              n_inputs integer,
              initial_gold_in_retrieval integer,
              gold_kept integer,
              gold_top1_after_qa integer,
              avg_initial_rank numeric,
              avg_post_qa_rank numeric,
              avg_rounds numeric,
              avg_active_count numeric,
              provider_calls_used integer NOT NULL DEFAULT 0,
              errors integer NOT NULL DEFAULT 0
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {KG_SCHEMA}.e2e_eval_results (
              id bigserial PRIMARY KEY,
              run_id bigint NOT NULL REFERENCES {KG_SCHEMA}.e2e_eval_runs(id) ON DELETE CASCADE,
              gold_id integer NOT NULL,
              source_id text NOT NULL,
              persona text NOT NULL,
              query text NOT NULL,
              expected_code text NOT NULL,
              retrieval_run_label text NOT NULL,
              question_mode text NOT NULL,
              answerer text NOT NULL,
              initial_rank integer,
              initial_top_codes text[] NOT NULL DEFAULT ARRAY[]::text[],
              post_qa_rank integer,
              post_qa_top_codes text[] NOT NULL DEFAULT ARRAY[]::text[],
              gold_in_retrieval boolean NOT NULL DEFAULT false,
              gold_kept boolean NOT NULL DEFAULT false,
              gold_top1_after_qa boolean NOT NULL DEFAULT false,
              rounds integer NOT NULL DEFAULT 0,
              active_count integer NOT NULL DEFAULT 0,
              out_count integer NOT NULL DEFAULT 0,
              cache_hit_count integer NOT NULL DEFAULT 0,
              cache_write_count integer NOT NULL DEFAULT 0,
              provider_calls_used integer NOT NULL DEFAULT 0,
              latency_seconds numeric,
              question_trace jsonb NOT NULL DEFAULT '[]'::jsonb,
              final_state jsonb NOT NULL DEFAULT '{{}}'::jsonb,
              error text,
              created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS e2e_eval_results_run_idx ON {KG_SCHEMA}.e2e_eval_results(run_id)"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS e2e_eval_results_mode_idx ON {KG_SCHEMA}.e2e_eval_results(question_mode, answerer)"
        )
    conn.commit()


def load_gold_rows(conn, *, pair_limit: int, personas: list[str]) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH pairs AS (
              SELECT source_id, expected_code, min(id) AS first_gold_id
              FROM {KG_SCHEMA}.eval_gold
              WHERE active AND source_type = 'atar' AND source_id IS NOT NULL
              GROUP BY source_id, expected_code
              ORDER BY min(id)
              LIMIT %s
            )
            SELECT g.id, g.source_id, g.source_type, g.persona, g.query, g.expected_code,
                   e.body AS oracle_text, p.first_gold_id
            FROM pairs p
            JOIN {KG_SCHEMA}.eval_gold g
              ON g.source_id = p.source_id AND g.expected_code = p.expected_code
            LEFT JOIN {KG_SCHEMA}.kg_edges e ON e.id = g.source_id
            WHERE g.active AND g.persona = ANY(%s)
            ORDER BY p.first_gold_id, g.persona, g.id
            """,
            (pair_limit, personas),
        )
        return [dict(r) for r in cur.fetchall()]


def create_run(conn, *, run_label: str, retrieval_run_label: str, question_mode: str, answerer: str,
               question_model: str, simulator_model: str, pair_limit: int, persona_count: int,
               input_count: int, retrieval_limit: int, hydrate_limit: int, max_rounds: int,
               allow_spend: bool, config: dict) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {KG_SCHEMA}.e2e_eval_runs
              (run_label, retrieval_run_label, question_mode, answerer, question_model,
               simulator_model, pair_limit, persona_count, input_count, retrieval_limit,
               hydrate_limit, max_rounds, allow_spend, config_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            RETURNING id
            """,
            (
                run_label, retrieval_run_label, question_mode, answerer, question_model,
                simulator_model, pair_limit, persona_count, input_count, retrieval_limit,
                hydrate_limit, max_rounds, allow_spend, json.dumps(config, default=str),
            ),
        )
        run_id = int(cur.fetchone()["id"])
    conn.commit()
    return run_id


def insert_result(conn, run_id: int, row: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {KG_SCHEMA}.e2e_eval_results
              (run_id, gold_id, source_id, persona, query, expected_code,
               retrieval_run_label, question_mode, answerer, initial_rank, initial_top_codes,
               post_qa_rank, post_qa_top_codes, gold_in_retrieval, gold_kept,
               gold_top1_after_qa, rounds, active_count, out_count, cache_hit_count,
               cache_write_count, provider_calls_used, latency_seconds, question_trace,
               final_state, error)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::text[],%s,%s::text[],%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
            """,
            (
                run_id, row["gold_id"], row["source_id"], row["persona"], row["query"], row["expected_code"],
                row["retrieval_run_label"], row["question_mode"], row["answerer"], row.get("initial_rank"),
                row.get("initial_top_codes") or [], row.get("post_qa_rank"), row.get("post_qa_top_codes") or [],
                bool(row.get("gold_in_retrieval")), bool(row.get("gold_kept")), bool(row.get("gold_top1_after_qa")),
                int(row.get("rounds") or 0), int(row.get("active_count") or 0), int(row.get("out_count") or 0),
                int(row.get("cache_hit_count") or 0), int(row.get("cache_write_count") or 0),
                int(row.get("provider_calls_used") or 0), row.get("latency_seconds"),
                json.dumps(row.get("question_trace") or [], default=str),
                json.dumps(row.get("final_state") or {}, default=str), row.get("error"),
            ),
        )
    conn.commit()


def finalize_run(conn, run_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH agg AS (
              SELECT count(*) AS n_inputs,
                     count(*) FILTER (WHERE initial_rank IS NOT NULL) AS initial_gold_in_retrieval,
                     count(*) FILTER (WHERE gold_kept) AS gold_kept,
                     count(*) FILTER (WHERE gold_top1_after_qa) AS gold_top1_after_qa,
                     avg(initial_rank) FILTER (WHERE initial_rank IS NOT NULL) AS avg_initial_rank,
                     avg(post_qa_rank) FILTER (WHERE post_qa_rank IS NOT NULL) AS avg_post_qa_rank,
                     avg(rounds) AS avg_rounds,
                     avg(active_count) AS avg_active_count,
                     sum(provider_calls_used) AS provider_calls_used,
                     count(*) FILTER (WHERE error IS NOT NULL) AS errors
              FROM {KG_SCHEMA}.e2e_eval_results
              WHERE run_id = %s
            )
            UPDATE {KG_SCHEMA}.e2e_eval_runs r
            SET finished_at = now(),
                n_inputs = agg.n_inputs,
                initial_gold_in_retrieval = agg.initial_gold_in_retrieval,
                gold_kept = agg.gold_kept,
                gold_top1_after_qa = agg.gold_top1_after_qa,
                avg_initial_rank = agg.avg_initial_rank,
                avg_post_qa_rank = agg.avg_post_qa_rank,
                avg_rounds = agg.avg_rounds,
                avg_active_count = agg.avg_active_count,
                provider_calls_used = COALESCE(agg.provider_calls_used, 0),
                errors = agg.errors
            FROM agg
            WHERE r.id = %s
            RETURNING r.*
            """,
            (run_id, run_id),
        )
        result = dict(cur.fetchone())
    conn.commit()
    return result


def choose_gold_option(question_hint: dict, expected_code: str) -> tuple[str, dict | None, dict]:
    exp = _flat(expected_code)
    options = question_hint.get("options") or []
    metas = question_hint.get("options_meta") or []
    none_meta = None
    for meta in metas:
        if str(meta.get("value") or "") == "__none__" or "none of these" in str(meta.get("label") or "").lower():
            none_meta = meta
        codes = {_flat(str(code)) for code in meta.get("codes") or []}
        if exp and exp in codes:
            return str(meta.get("label") or options[0]), meta, {"source": "gold_option", "matched_gold_option": True}
    if none_meta:
        return str(none_meta.get("label") or options[-1]), none_meta, {"source": "gold_option", "matched_gold_option": False, "fallback": "none"}
    if options:
        return str(options[0]), (metas[0] if metas else None), {"source": "gold_option", "matched_gold_option": False, "fallback": "first"}
    return "", None, {"source": "gold_option", "matched_gold_option": False, "fallback": "no_options"}


async def choose_llm_trader_answer(*, client, session, query: str, oracle_text: str, question_hint: dict, round_number: int) -> tuple[str, dict | None, dict]:
    options = [str(opt) for opt in (question_hint.get("options") or [])]
    sim = await qa_loop.simulate_trader_answer(
        client, session, query, str(question_hint.get("question") or ""), options, round_number, oracle_text=oracle_text,
    )
    chosen = str(sim.get("chosen") or "")
    meta = None
    for item in question_hint.get("options_meta") or []:
        if str(item.get("label") or "") == chosen:
            meta = item
            break
    return chosen, meta, {"source": "llm_trader", **sim}


@dataclass
class EvalContext:
    retrieval_run_label: str
    retrieval_config: dict
    api_key: str | None
    retrieval_limit: int
    hydrate_limit: int
    max_rounds: int
    question_mode: str
    question_model: str
    answerer: str
    allow_spend: bool
    simulator_client: Any | None


async def evaluate_row(gold: dict, ctx: EvalContext) -> dict:
    from experiment_retrieval import retrieve_for_config
    import main as product_main

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
        top_codes = [_candidate_code(c) for c in candidates]
        initial_rank = _rank_of(top_codes, gold["expected_code"])
        result.update({
            "initial_rank": initial_rank,
            "initial_top_codes": top_codes[:50],
            "gold_in_retrieval": initial_rank is not None,
        })
        if not candidates:
            result["error"] = "no retrieval candidates"
            return result

        qa_history: list[dict] = []
        session = qa_loop.SessionFacts()
        final_hydration: dict | None = None
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
            if not active_codes or _rank_of(active_codes, gold["expected_code"]) is None:
                break
            if len(active_codes) <= 1:
                break
            question_hint = hydration.get("question_hint") or {}
            options = question_hint.get("options") or []
            if not question_hint or not options:
                break
            if ctx.answerer == "llm_trader":
                if ctx.simulator_client is None:
                    raise RuntimeError("llm_trader answerer requires provider client")
                answer, meta, answer_debug = await choose_llm_trader_answer(
                    client=ctx.simulator_client,
                    session=session,
                    query=gold["query"],
                    oracle_text=str(gold.get("oracle_text") or ""),
                    question_hint=question_hint,
                    round_number=round_number,
                )
                result["provider_calls_used"] += 1
            else:
                answer, meta, answer_debug = choose_gold_option(question_hint, gold["expected_code"])
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
            result["question_trace"].append({
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
                result["provider_calls_used"] += 1

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
        active_codes = [str(code) for code in (state.get("in_scope_codes") or [])]
        post_rank = _rank_of(active_codes, gold["expected_code"])
        result.update({
            "post_qa_rank": post_rank,
            "post_qa_top_codes": active_codes[:50],
            "gold_kept": post_rank is not None,
            "gold_top1_after_qa": post_rank == 1,
            "rounds": len(qa_history),
            "active_count": len(active_codes),
            "out_count": int(state.get("out_of_scope_count") or 0),
            "cache_hit_count": int(final_hydration.get("cache_hit_count") or 0),
            "cache_write_count": int(final_hydration.get("cache_write_count") or 0),
            "final_state": {
                "qa_state": state,
                "coverage_totals": final_hydration.get("coverage_totals") or {},
                "lexical_specificity": final_hydration.get("lexical_specificity") or {},
                "query_difficulty": final_hydration.get("query_difficulty") or {},
                "active_query_difficulty": final_hydration.get("active_query_difficulty") or {},
                "leg_counts": leg_counts,
            },
        })
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        result["latency_seconds"] = round(time.time() - started, 3)
    return result


async def run_mode(conn, *, run_label: str, gold_rows: list[dict], retrieval_run_label: str,
                   retrieval_config: dict, retrieval_limit: int, hydrate_limit: int,
                   max_rounds: int, question_mode: str, question_model: str,
                   answerer: str, allow_spend: bool, pair_limit: int,
                   personas: list[str], concurrency: int) -> dict:
    if answerer == "llm_trader":
        if not allow_spend or not openai_allowed() or not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("llm_trader requires --allow-spend plus CLASSIFICATION_ALLOW_PROVIDER_CALLS=1 and OPENAI_API_KEY")
        from openai import AsyncOpenAI
        qa_loop.SIMULATOR_MODEL = os.environ.get("QA_SIMULATOR_MODEL", "gpt-5-mini")
        simulator_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
        simulator_model = qa_loop.SIMULATOR_MODEL
    else:
        simulator_client = None
        simulator_model = None

    run_id = create_run(
        conn,
        run_label=run_label,
        retrieval_run_label=retrieval_run_label,
        question_mode=question_mode,
        answerer=answerer,
        question_model=question_model,
        simulator_model=simulator_model or "",
        pair_limit=pair_limit,
        persona_count=len(personas),
        input_count=len(gold_rows),
        retrieval_limit=retrieval_limit,
        hydrate_limit=hydrate_limit,
        max_rounds=max_rounds,
        allow_spend=allow_spend,
        config={"retrieval_config": retrieval_config, "personas": personas},
    )
    ctx = EvalContext(
        retrieval_run_label=retrieval_run_label,
        retrieval_config=retrieval_config,
        api_key=os.environ.get("OPENAI_API_KEY"),
        retrieval_limit=retrieval_limit,
        hydrate_limit=hydrate_limit,
        max_rounds=max_rounds,
        question_mode=question_mode,
        question_model=question_model,
        answerer=answerer,
        allow_spend=allow_spend,
        simulator_client=simulator_client,
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
        if done % 25 == 0 or done == len(tasks):
            print(json.dumps({"run_id": run_id, "question_mode": question_mode, "done": done, "total": len(tasks)}), flush=True)
    summary = finalize_run(conn, run_id)
    print(json.dumps({"run_id": run_id, "summary": summary}, default=str), flush=True)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Run fixed-set hydrated E2E matrix")
    parser.add_argument("--run-label", default="hydrated_e2e")
    parser.add_argument("--retrieval-run-label", default="baseline_fts_only")
    parser.add_argument("--pair-limit", type=int, default=100)
    parser.add_argument("--personas", default=",".join(DEFAULT_PERSONAS))
    parser.add_argument("--retrieval-limit", type=int, default=100)
    parser.add_argument("--hydrate-limit", type=int, default=100)
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--question-modes", default=",".join(QUESTION_MODES))
    parser.add_argument("--question-model", default="gpt-5-nano")
    parser.add_argument("--answerer", choices=["gold_option", "llm_trader"], default="gold_option")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--allow-spend", action="store_true")
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
        raise SystemExit("selected retrieval experiment requires provider calls; pass --allow-spend with an approved budget")

    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        ensure_tables(conn)
        gold_rows = load_gold_rows(conn, pair_limit=args.pair_limit, personas=personas)
        print(json.dumps({"gold_rows": len(gold_rows), "pair_limit": args.pair_limit, "personas": personas, "retrieval_run_label": args.retrieval_run_label}))
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
                )

        asyncio.run(run_all_modes())


if __name__ == "__main__":
    main()
