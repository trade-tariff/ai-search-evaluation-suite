"""Classification matrix harness - the disambiguation analogue of run_eval's
retrieval matrix.

Runs a single CONFIG = {strategy, prompt_mode, augmentation, model, candidate_limit}
across personas, driving the full Q&A loop (qa_loop.run_qa_session) per (gold
ATAR, persona) with the ATAR description as oracle, LOO-honest. Each (config,
persona) cell is tagged with a run_label + the exact config_json so the
/eval/classify-matrix view can render config x persona -> gold-in-set %.

PRIMARY METRIC: gold_in_final_set (PRESENCE - is the gold anywhere in the final
committed/surviving set). The whole point of the ELIMINATE strategy is to stop
the converge loop dropping retrievable golds, so presence matters more than rank.

Also records: gold rank in the final set, survivor-set size, rounds, classify_calls,
simulator_calls, and an ESTIMATED $/session (per-call price x calls; labelled est).

Writes one row per session to kg.classify_runs. Idempotent table create.

Usage (single config):
  .venv/bin/python -m journey.run_classify_matrix \
      --run-label baseline_converge \
      --strategy converge --prompt-mode baseline --augmentation facts+kg \
      --model gpt-5-mini --candidate-limit 40 \
      --personas naive_vague --limit 5 --concurrency 4

Or run a built-in sweep of (strategy x prompt_mode x augmentation x model) with
--sweep (see SWEEP_GRID); each combination becomes its own run_label.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from statistics import median

from dotenv import load_dotenv

_env_candidates = [
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path.cwd() / ".env",
    Path(__file__).resolve().parents[2] / ".env",
    Path(__file__).resolve().parents[3] / ".env",
]
for _p in [p for p in _env_candidates if p is not None]:
    if _p is not None and _p.exists():
        load_dotenv(_p)
        break

import psycopg
from psycopg.rows import dict_row

from . import qa_loop
from .run_eval import _norm_code, build_loo_map

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")

# --- gold-id allowlist (experiment variant) -------------------------------
# GOLD_IDS = comma-separated kg.eval_gold ids. When set, run_config restricts
# its gold SELECT to exactly these ids (and ignores --personas/--limit framing
# for membership, though they still apply as additional WHERE clauses).
_GOLD_IDS_ENV = os.environ.get("GOLD_IDS", "").strip()
GOLD_IDS = [int(x) for x in _GOLD_IDS_ENV.split(",") if x.strip()] if _GOLD_IDS_ENV else None

# Augmentation presets -> (use_facets, kg_include-on?). 'none' strips both the
# facet matrix and the KG edges from the classify prompt; 'facts'/'kg'/'facts+kg'
# toggle each. Retrieval legs are held at the Exp 2+3 production_v2 winner so the
# only classify-time variable is the prompt augmentation.
AUGMENTATIONS = {
    "none": {"use_facets": False, "kg_on": False},
    "facts": {"use_facets": True, "kg_on": False},
    "kg": {"use_facets": False, "kg_on": True},
    "facts+kg": {"use_facets": True, "kg_on": True},
}

_KG_INCLUDE_ON = {
    "chapter_notes": True, "section_notes": True, "legacy_blob_notes": False,
    "girs": True, "atar_rationales": True, "heading_rules": True, "other_global": True,
}
_KG_INCLUDE_OFF = {k: False for k in _KG_INCLUDE_ON}

# Estimated per-classify-call price ($). Used only for the est $/session column;
# real token usage is not threaded out of classification.py. Defaults are rough
# gpt-5.5-class costs; override per model via env if needed.
_CLASSIFY_CALL_USD = {
    "gpt-5-mini": float(os.environ.get("CLASSIFY_CALL_USD_MINI", "0.004")),
    "gpt-5.5": float(os.environ.get("CLASSIFY_CALL_USD_55", "0.02")),
}
_SIM_CALL_USD = float(os.environ.get("SIM_CALL_USD", "0.003"))  # gpt-5-mini simulator


def _est_session_cost(model: str, classify_calls: int, sim_calls: int) -> float:
    cc = _CLASSIFY_CALL_USD.get(model, 0.01)
    return round(classify_calls * cc + sim_calls * _SIM_CALL_USD, 5)


def build_config(strategy: str, prompt_mode: str, augmentation: str,
                 candidate_limit: int, fact_excl, edge_excl) -> dict:
    aug = AUGMENTATIONS[augmentation]
    provider_calls_enabled = os.environ.get("JOURNEY_ALLOW_PROVIDER_CALLS", "").strip() == "1"
    return {
        "strategy": strategy,
        "prompt_mode": prompt_mode,
        "use_llm_candidate_selection": provider_calls_enabled,
        "candidate_selection_model": os.environ.get("CLASSIFY_LLM_MODEL", "gpt-5-mini"),
        "use_query_expansion": True,
        "use_facets": aug["use_facets"],
        "use_session_facts": True,
        "use_entropy_picker": strategy == "converge",  # eliminate has its own LLM pass
        "kg_include": dict(_KG_INCLUDE_ON if aug["kg_on"] else _KG_INCLUDE_OFF),
        "retrieval": {
            "use_curated": False,
            "use_vector": False,
            "use_facts_leg": True,
            "use_kg_context_leg": True,
            "use_facts_vec_leg": True,
            "use_kg_vec_leg": True,
            "facts_cap": 0.5, "kg_cap": 0.5,
            "facts_vec_cap": 0.9, "kg_vec_cap": 0.9,
            "rrf_k": 60,
            "limit": candidate_limit,
            "exclude_fact_sources": fact_excl,
            "exclude_edge_ids": edge_excl,
        },
    }


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS kg.classify_runs (
                id               bigserial PRIMARY KEY,
                started_at       timestamp NOT NULL DEFAULT now(),
                run_label        text NOT NULL,
                config_json      jsonb NOT NULL,
                strategy         text NOT NULL,
                prompt_mode      text NOT NULL,
                augmentation     text NOT NULL,
                model            text NOT NULL,
                candidate_limit  int  NOT NULL,
                gold_id          bigint NOT NULL,
                source_id        text NOT NULL,
                persona          text NOT NULL,
                query            text NOT NULL,
                expected_code    text NOT NULL,
                rounds           int NOT NULL,
                final_mode       text NOT NULL,
                final_top1       text,
                final_set        text[],
                gold_in_final_set boolean,   -- PRIMARY metric: presence anywhere in final set
                gold_in_top1     boolean,
                gold_in_top5     boolean,
                gold_rank        int,        -- 1-based rank of gold in final set, NULL if absent
                survivor_set_size int,
                classify_calls   int,
                simulator_calls  int,
                simulator_failed boolean DEFAULT false,
                est_cost_usd     numeric(10,5),
                latency_seconds  numeric(8,2),
                trace_json       jsonb
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS classify_runs_label_persona "
            "ON kg.classify_runs (run_label, persona)"
        )
    conn.commit()


async def run_one_session(gold: dict, oracle_text: str, *, strategy: str,
                          prompt_mode: str, augmentation: str, model: str,
                          candidate_limit: int, max_rounds: int,
                          loo_map: dict[str, tuple[list[str], list[str]]] | None = None) -> dict:
    fact_excl, edge_excl = (loo_map or {}).get(_norm_code(gold.get("expected_code")), ([], []))
    config = build_config(strategy, prompt_mode, augmentation, candidate_limit, fact_excl, edge_excl)

    started = time.time()
    result = await qa_loop.run_qa_session(
        query=gold["query"], max_rounds=max_rounds,
        oracle_text=oracle_text, config=config,
    )
    elapsed = time.time() - started

    expected = gold["expected_code"]

    # Final SET = the full committed/surviving answer list (not truncated to 5).
    # For eliminate this is survivors_final; for converge it's final_answers.
    final_objs = result.get("survivors_final") or result.get("final_answers") or []
    final_set = [a.get("commodity_code", "") for a in final_objs]
    if not final_set and result.get("candidates_final_round"):
        final_set = [c["commodity_code"] for c in result["candidates_final_round"]]

    final_top1 = final_set[0] if final_set else None
    gold_in_final_set = expected in final_set
    gold_rank = (final_set.index(expected) + 1) if gold_in_final_set else None
    gold_in_top1 = final_top1 == expected if final_top1 else False
    gold_in_top5 = expected in final_set[:5]
    survivor_set_size = result.get("survivor_count", len(final_set))

    classify_calls = result.get("total_classify_calls", 0)
    sim_calls = result.get("total_simulator_calls", 0)

    return {
        "gold_id": gold["id"], "source_id": gold["source_id"], "persona": gold["persona"],
        "query": gold["query"], "expected_code": expected,
        "rounds": len(result.get("rounds", [])),
        "final_mode": result.get("final_mode", "?"),
        "final_top1": final_top1, "final_set": final_set,
        "gold_in_final_set": gold_in_final_set,
        "gold_in_top1": gold_in_top1, "gold_in_top5": gold_in_top5,
        "gold_rank": gold_rank, "survivor_set_size": survivor_set_size,
        "classify_calls": classify_calls, "simulator_calls": sim_calls,
        "simulator_failed": result.get("final_mode") == "simulator_failed",
        "est_cost_usd": _est_session_cost(model, classify_calls, sim_calls),
        "latency_seconds": round(elapsed, 2),
        "config": config,
        "trace_json": result,
    }


async def run_config(conn, *, run_label: str, strategy: str, prompt_mode: str,
                     augmentation: str, model: str, candidate_limit: int,
                     personas: list[str], limit: int | None, concurrency: int,
                     max_rounds: int) -> list[dict]:
    os.environ["CLASSIFY_LLM_MODEL"] = model  # both classify + eliminate read this

    with conn.cursor() as cur:
        if GOLD_IDS:
            # Experiment mode: exactly the allowlisted gold ids, ignore persona/limit framing.
            cur.execute(
                "SELECT id, source_id, source_type, persona, query, expected_code "
                "FROM kg.eval_gold WHERE source_type='atar' AND id = ANY(%s) ORDER BY id",
                (GOLD_IDS,),
            )
            gold_rows = [dict(r) for r in cur.fetchall()]
            print(f"[{run_label}] GOLD_IDS allowlist -> {len(gold_rows)} rows")
            return await _run_rows(conn, gold_rows, run_label=run_label, strategy=strategy,
                                   prompt_mode=prompt_mode, augmentation=augmentation,
                                   model=model, candidate_limit=candidate_limit,
                                   concurrency=concurrency, max_rounds=max_rounds)
        sql = (
            "SELECT id, source_id, source_type, persona, query, expected_code FROM kg.eval_gold "
            "WHERE source_type='atar' AND persona = ANY(%s) ORDER BY id"
        )
        params: list = [personas]
        if limit:
            # N evenly-from-the-top golds per persona (deterministic, cheap dry run)
            sql = (
                "SELECT * FROM (SELECT id, source_id, source_type, persona, query, expected_code, "
                "row_number() OVER (PARTITION BY persona ORDER BY id) rn FROM kg.eval_gold "
                "WHERE source_type='atar' AND persona = ANY(%s)) t WHERE rn <= %s ORDER BY id"
            )
            params.append(limit)
        cur.execute(sql, tuple(params))
        gold_rows = [dict(r) for r in cur.fetchall()]

    return await _run_rows(conn, gold_rows, run_label=run_label, strategy=strategy,
                           prompt_mode=prompt_mode, augmentation=augmentation,
                           model=model, candidate_limit=candidate_limit,
                           concurrency=concurrency, max_rounds=max_rounds)

async def _run_rows(conn, gold_rows, *, run_label, strategy, prompt_mode,
                    augmentation, model, candidate_limit, concurrency, max_rounds):
    os.environ['CLASSIFY_LLM_MODEL'] = model
    loo_map = build_loo_map(conn, gold_rows)
    atar_ids = sorted({g["source_id"] for g in gold_rows})
    with conn.cursor() as cur:
        cur.execute("SELECT id, body FROM kg.kg_edges WHERE id = ANY(%s)", (atar_ids,))
        oracles = {r["id"]: r["body"] for r in cur.fetchall()}

    print(f"[{run_label}] {len(gold_rows)} sessions | strategy={strategy} "
          f"prompt_mode={prompt_mode} aug={augmentation} model={model} limit={candidate_limit}")

    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []

    async def process(gold):
        async with sem:
            oracle = oracles.get(gold["source_id"]) or ""
            try:
                return await run_one_session(
                    gold, oracle, strategy=strategy, prompt_mode=prompt_mode,
                    augmentation=augmentation, model=model,
                    candidate_limit=candidate_limit, max_rounds=max_rounds,
                    loo_map=loo_map,
                )
            except Exception as exc:
                print(f"  [error] {gold['source_id']} {gold['persona']}: {exc!r}")
                return None

    tasks = [process(g) for g in gold_rows]
    for task in asyncio.as_completed(tasks):
        res = await task
        if res is None:
            continue
        results.append(res)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kg.classify_runs
                  (run_label, config_json, strategy, prompt_mode, augmentation, model,
                   candidate_limit, gold_id, source_id, persona, query, expected_code,
                   rounds, final_mode, final_top1, final_set, gold_in_final_set,
                   gold_in_top1, gold_in_top5, gold_rank, survivor_set_size,
                   classify_calls, simulator_calls, simulator_failed, est_cost_usd,
                   latency_seconds, trace_json)
                VALUES (%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s::jsonb)
                """,
                (
                    run_label, json.dumps(res["config"]), strategy, prompt_mode, augmentation,
                    model, candidate_limit, res["gold_id"], res["source_id"], res["persona"],
                    res["query"], res["expected_code"], res["rounds"], res["final_mode"],
                    res["final_top1"], res["final_set"], res["gold_in_final_set"],
                    res["gold_in_top1"], res["gold_in_top5"], res["gold_rank"],
                    res["survivor_set_size"], res["classify_calls"], res["simulator_calls"],
                    res["simulator_failed"], res["est_cost_usd"], res["latency_seconds"],
                    json.dumps(res["trace_json"]),
                ),
            )
            conn.commit()
    return results



def _print_summary(run_label: str, results: list[dict]) -> None:
    by_persona: dict[str, list[dict]] = {}
    for r in results:
        by_persona.setdefault(r["persona"], []).append(r)
    print()
    print("=" * 78)
    print(f"CLASSIFY MATRIX - {run_label}")
    print("=" * 78)
    print(f"{'persona':<16} {'n':>3} {'in_set':>7} {'top1':>6} {'top5':>6} "
          f"{'med_rk':>6} {'med_sz':>6} {'med_rd':>6} {'$/sess':>8}")
    for persona in sorted(by_persona):
        rows = by_persona[persona]
        n = len(rows)
        in_set = sum(1 for r in rows if r["gold_in_final_set"]) / n
        top1 = sum(1 for r in rows if r["gold_in_top1"]) / n
        top5 = sum(1 for r in rows if r["gold_in_top5"]) / n
        ranks = [r["gold_rank"] for r in rows if r["gold_rank"]]
        med_rk = median(ranks) if ranks else 0
        med_sz = median(r["survivor_set_size"] for r in rows)
        med_rd = median(r["rounds"] for r in rows)
        cost = sum(r["est_cost_usd"] for r in rows) / n
        print(f"{persona:<16} {n:>3} {in_set:>6.1%} {top1:>5.1%} {top5:>5.1%} "
              f"{med_rk:>6.1f} {med_sz:>6.1f} {med_rd:>6.1f} {cost:>7.4f}")


# Full-matrix sweep grid (run later on the 116-CC corpus). Each tuple = one
# run_label. Personas/limit/model passed on the CLI apply to every combination.
SWEEP_GRID = [
    ("baseline", "facts+kg"),
    ("rule_reasoning", "facts+kg"),
    ("exclusion_aware", "facts+kg"),
    ("gir_citation", "facts+kg"),
    ("self_verify", "facts+kg"),
    ("baseline", "none"),
    ("baseline", "facts"),
    ("baseline", "kg"),
]


async def main() -> None:
    ap = argparse.ArgumentParser(description="Classification matrix harness")
    ap.add_argument("--run-label", help="Tag for this run (one config). Required unless --sweep.")
    ap.add_argument("--strategy", choices=["converge", "eliminate"], default="converge")
    ap.add_argument("--prompt-mode", choices=sorted(
        {"baseline", "rule_reasoning", "exclusion_aware", "gir_citation", "self_verify"}),
        default="baseline")
    ap.add_argument("--augmentation", choices=sorted(AUGMENTATIONS), default="facts+kg")
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--candidate-limit", type=int, default=40)
    ap.add_argument("--personas", default="naive_vague",
                    help="Comma-separated persona list.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap golds per persona (deterministic, for dry runs).")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--max-rounds", type=int, default=int(os.environ.get("EXP6_MAX_ROUNDS", "5")))
    ap.add_argument("--sweep", action="store_true",
                    help="Run SWEEP_GRID across BOTH strategies (each combo -> own run_label).")
    args = ap.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr)
        sys.exit(1)

    personas = [p.strip() for p in args.personas.split(",") if p.strip()]
    conn = psycopg.connect(DSN, row_factory=dict_row)
    _ensure_table(conn)

    if args.sweep:
        combos = []
        for strategy in ("converge", "eliminate"):
            for prompt_mode, aug in SWEEP_GRID:
                label = f"{strategy}__{prompt_mode}__{aug}__{args.model}"
                combos.append((label, strategy, prompt_mode, aug))
        print(f"SWEEP: {len(combos)} run_labels x {len(personas)} personas")
    else:
        if not args.run_label:
            print("--run-label required (or use --sweep)", file=sys.stderr)
            sys.exit(1)
        combos = [(args.run_label, args.strategy, args.prompt_mode, args.augmentation)]

    started = time.time()
    for label, strategy, prompt_mode, aug in combos:
        results = await run_config(
            conn, run_label=label, strategy=strategy, prompt_mode=prompt_mode,
            augmentation=aug, model=args.model, candidate_limit=args.candidate_limit,
            personas=personas, limit=args.limit, concurrency=args.concurrency,
            max_rounds=args.max_rounds,
        )
        _print_summary(label, results)
    print(f"\nTotal wall time: {time.time() - started:.1f}s")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
