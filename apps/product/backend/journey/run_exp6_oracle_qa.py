"""Exp 6: Oracle Q&A upper bound.

For each (gold ATAR, naive paraphrase) pair, run the full qa_loop with the
ATAR's product description as oracle_text. Measures how often the system
converges to the gold code within N rounds when the simulator has perfect
ground truth.

This is the CEILING of what the Q&A loop can achieve given the current
retrieval candidate set. If we hit 70%+ here, the architecture is sound and
the remaining gap to production is just operationalisation. If we underperform
the retrieval recall@K curve, the Q&A loop itself has a structural problem.

Honest: applies LOO exclusion (each ATAR's own facts/edge are excluded from
retrieval) so we measure generalisation, not memorisation.

Reads from kg.eval_gold. Writes to kg.exp6_qa_runs (one row per session) and
prints a summary table.

Cost: each session uses 1-3 gpt-5.5 classify calls + 1-3 gpt-5-mini simulator
calls. Roughly $0.05-$0.15 per session, $3-10 total for 70-210 sessions.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from statistics import median

from dotenv import load_dotenv

for p in [Path(__file__).parent / ".env",
          Path(__file__).parent.parent / ".env",
          Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row

from . import qa_loop
from .run_eval import _loo_exclusions

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MAX_ROUNDS = int(os.environ.get("EXP6_MAX_ROUNDS", "5"))
PERSONAS = os.environ.get("EXP6_PERSONAS", "naive_vague").split(",")
CONCURRENCY = int(os.environ.get("EXP6_CONCURRENCY", "4"))


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS kg.exp6_qa_runs (
                id            bigserial PRIMARY KEY,
                started_at    timestamp NOT NULL DEFAULT now(),
                gold_id       bigint NOT NULL,
                source_id     text NOT NULL,
                persona       text NOT NULL,
                query         text NOT NULL,
                expected_code text NOT NULL,
                rounds        int NOT NULL,
                final_mode    text NOT NULL,
                final_top1    text,
                final_top5    text[],
                gold_in_top1  boolean,
                gold_in_top5  boolean,
                classify_calls int,
                simulator_calls int,
                simulator_failed boolean DEFAULT false,
                latency_seconds numeric(8,2),
                trace_json    jsonb
            )
            """
        )
    conn.commit()


def _atar_oracle_text(conn, source_id: str) -> str | None:
    """Fetch the ATAR rationale body to use as authoritative oracle."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT body FROM kg.kg_edges WHERE id = %s",
            (source_id,),
        )
        row = cur.fetchone()
    return row["body"] if row else None


async def run_one_session(gold: dict, oracle_text: str) -> dict:
    """Run one Q&A session under LOO + production_v2 retrieval defaults."""
    fact_excl, edge_excl = _loo_exclusions(gold.get("source_id"))
    # Use the production_v2 retrieval defaults (Exp 2+3 winner) + LOO exclusions
    config = {
        "use_query_expansion": True,
        "use_facets": True,
        "retrieval": {
            "use_curated": False,
            "use_vector": os.environ.get("EXP6_USE_VECTOR", "0") == "1",
            "use_vec_adapter": os.environ.get("EXP6_USE_VEC_ADAPTER", "0") == "1",
            "use_facts_leg": True,
            "use_kg_context_leg": True,
            "use_facts_vec_leg": True,
            "use_kg_vec_leg": True,
            "facts_cap": 0.5,
            "kg_cap": 0.5,
            "facts_vec_cap": 0.9,
            "kg_vec_cap": 0.9,
            "rrf_k": 60,
            "limit": int(os.environ.get("EXP6_LIMIT", "40")),  # candidate set fed to LLM (Q&A round)
            "exclude_fact_sources": fact_excl,
            "exclude_edge_ids": edge_excl,
        },
    }
    started = time.time()
    result = await qa_loop.run_qa_session(
        query=gold["query"],
        max_rounds=MAX_ROUNDS,
        oracle_text=oracle_text,
        config=config,
    )
    elapsed = time.time() - started

    expected = gold["expected_code"]
    final_top5: list[str] = []
    final_top1: str | None = None
    if result.get("final_answers"):
        final_top5 = [a.get("commodity_code", "") for a in result["final_answers"][:5]]
        final_top1 = final_top5[0] if final_top5 else None
    elif result.get("candidates_final_round"):
        final_top5 = [c["commodity_code"] for c in result["candidates_final_round"][:5]]
        final_top1 = final_top5[0] if final_top5 else None

    gold_in_top1 = (final_top1 == expected) if final_top1 else False
    gold_in_top5 = expected in final_top5

    return {
        "gold_id": gold["id"],
        "source_id": gold["source_id"],
        "persona": gold["persona"],
        "query": gold["query"],
        "expected_code": expected,
        "rounds": len(result.get("rounds", [])),
        "final_mode": result.get("final_mode", "?"),
        "final_top1": final_top1,
        "final_top5": final_top5,
        "gold_in_top1": gold_in_top1,
        "gold_in_top5": gold_in_top5,
        "classify_calls": result.get("total_classify_calls", 0),
        "simulator_calls": result.get("total_simulator_calls", 0),
        "simulator_failed": result.get("final_mode") == "simulator_failed",
        "latency_seconds": round(elapsed, 2),
        "trace_json": result,
    }


async def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY required", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(DSN, row_factory=dict_row)
    _ensure_table(conn)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, source_id, persona, query, expected_code
            FROM kg.eval_gold
            WHERE source_type = 'atar' AND persona = ANY(%s)
            ORDER BY id
            """,
            (PERSONAS,),
        )
        gold_rows = [dict(r) for r in cur.fetchall()]
    print(f"Loaded {len(gold_rows)} gold sessions (personas: {PERSONAS})")

    # Pre-fetch all oracle texts in one round-trip
    atar_ids = sorted({g["source_id"] for g in gold_rows})
    with conn.cursor() as cur:
        cur.execute("SELECT id, body FROM kg.kg_edges WHERE id = ANY(%s)", (atar_ids,))
        oracles = {r["id"]: r["body"] for r in cur.fetchall()}
    print(f"Loaded {len(oracles)} oracle texts")

    sem = asyncio.Semaphore(CONCURRENCY)
    results: list[dict] = []

    async def process(gold):
        async with sem:
            oracle = oracles.get(gold["source_id"]) or ""
            try:
                return await run_one_session(gold, oracle)
            except Exception as exc:
                print(f"  [error] {gold['source_id']} {gold['persona']}: {exc!r}")
                return None

    started = time.time()
    tasks = [process(g) for g in gold_rows]
    for i, task in enumerate(asyncio.as_completed(tasks), start=1):
        res = await task
        if res is None:
            continue
        results.append(res)
        # Stream-persist so a crash doesn't lose everything
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kg.exp6_qa_runs
                  (gold_id, source_id, persona, query, expected_code, rounds, final_mode,
                   final_top1, final_top5, gold_in_top1, gold_in_top5,
                   classify_calls, simulator_calls, simulator_failed,
                   latency_seconds, trace_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    res["gold_id"], res["source_id"], res["persona"], res["query"],
                    res["expected_code"], res["rounds"], res["final_mode"],
                    res["final_top1"], res["final_top5"],
                    res["gold_in_top1"], res["gold_in_top5"],
                    res["classify_calls"], res["simulator_calls"], res["simulator_failed"],
                    res["latency_seconds"], json.dumps(res["trace_json"]),
                ),
            )
            conn.commit()
        if i % 10 == 0:
            print(f"  {i}/{len(gold_rows)} done")

    elapsed = time.time() - started
    print(f"\nFinished {len(results)}/{len(gold_rows)} sessions in {elapsed:.1f}s")

    # Summary by persona
    by_persona: dict[str, list[dict]] = {}
    for r in results:
        by_persona.setdefault(r["persona"], []).append(r)

    print()
    print("=" * 70)
    print("EXP 6: ORACLE Q&A UPPER BOUND")
    print("=" * 70)
    print(f"{'persona':<18} {'n':>4} {'top1':>7} {'top5':>7} {'conv%':>7} {'med_rd':>7} {'mean_rd':>7} {'med_t':>7}")
    for persona in sorted(by_persona.keys()):
        rows = by_persona[persona]
        n = len(rows)
        top1 = sum(1 for r in rows if r["gold_in_top1"]) / n
        top5 = sum(1 for r in rows if r["gold_in_top5"]) / n
        converged = sum(1 for r in rows if r["final_mode"] == "answers") / n
        rounds_med = median(r["rounds"] for r in rows)
        rounds_mean = sum(r["rounds"] for r in rows) / n
        lat_med = median(r["latency_seconds"] for r in rows)
        print(f"{persona:<18} {n:>4} {top1:>6.1%} {top5:>6.1%} {converged:>6.1%} "
              f"{rounds_med:>7.1f} {rounds_mean:>7.2f} {lat_med:>6.1f}s")
    print()
    sim_fails = sum(1 for r in results if r["simulator_failed"])
    print(f"Simulator failures (out of {len(results)}): {sim_fails}")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
