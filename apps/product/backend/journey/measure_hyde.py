"""HyDE: does retrieving on a hypothetical tariff-style description lift recall@100?

For each vague trader query, generate a hypothetical product description in formal
HS/tariff language, retrieve on THAT, and UNION with the base (literal-query)
retrieval. The hypothetical doc bridges trader-vocabulary -> tariff-vocabulary at
retrieval time, pulling in golds the literal query never surfaces. Unlike rerank
(which only reorders what's already retrieved), HyDE can raise the @500 ceiling.

Base retrieval reused from the cached pool (rerank_pool_nv_500.json) so we don't
re-retrieve it. LOO-honest. Reports recall@K of base vs base+HyDE union.

Env: HYDE_MODEL(gpt-5-mini) HYDE_EFFORT(minimal) HYDE_N(0=all) HYDE_CONCURRENCY(8)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

for p in [Path(__file__).parent / ".env",
          Path(__file__).parent.parent / ".env",
          Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row
from openai import AsyncOpenAI

from . import local_db
from .run_eval import _loo_exclusions

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MODEL = os.environ.get("HYDE_MODEL", "gpt-5-mini")
EFFORT = os.environ.get("HYDE_EFFORT", "minimal")
N_LIMIT = int(os.environ.get("HYDE_N", "0"))
CONCURRENCY = int(os.environ.get("HYDE_CONCURRENCY", "8"))
POOL = 500
K_LIST = [5, 10, 20, 50, 100]
DATA = Path(__file__).parent / "data"
BASE_POOL = DATA / f"rerank_pool_nv_{POOL}.json"
HYDE_CACHE = DATA / "hyde_texts_nv.json"

RETRIEVE_KWARGS = dict(
    use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
    use_facts_vec=True, use_kg_vec=True, facts_vec_cap=0.9, kg_vec_cap=0.9,
)

_SYSTEM = """You write a short hypothetical product description in the formal language
of the UK / Harmonised System customs tariff, to help retrieve the right commodity
code. Given a trader's casual description, write 2-3 sentences describing the most
likely product using tariff-style terms (material, function, form, processing,
composition). Do not guess a code. Output the description text only."""


def _norm(code: str) -> str:
    return (code or "").replace(".", "")[:10]


async def gen_hyde(client, sem, query: str) -> str:
    async with sem:
        for attempt in range(3):
            try:
                extra = {"reasoning_effort": EFFORT} if MODEL.startswith("gpt-5") else {}
                resp = await client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": _SYSTEM},
                              {"role": "user", "content": f"Trader query: {query}"}],
                    **extra,
                )
                return (resp.choices[0].message.content or "").strip()
            except Exception as exc:
                if attempt == 2:
                    print(f"  [hyde gen failed: {exc!r}]", flush=True)
                    return ""
                await asyncio.sleep(1.5 * (attempt + 1))
    return ""


def union_recall(base_pool: list[dict], hyde_pool: list[dict], gold: str, k: int) -> bool:
    best: dict[str, float] = {}
    for c in base_pool:
        best[c["commodity_code"]] = max(best.get(c["commodity_code"], -1e9), float(c.get("score", 0.0)))
    for c in hyde_pool:
        best[c["commodity_code"]] = max(best.get(c["commodity_code"], -1e9), float(c.get("score", 0.0)))
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    g = _norm(gold)
    for idx, (code, _) in enumerate(ranked, start=1):
        if _norm(code) == g:
            return idx <= k
    return False


def first_rank(pool: list[dict], gold: str) -> int | None:
    g = _norm(gold)
    for idx, c in enumerate(pool, start=1):
        if _norm(c["commodity_code"]) == g:
            return idx
    return None


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr)
        sys.exit(1)
    if not BASE_POOL.exists():
        print(f"base pool cache missing: {BASE_POOL} (run measure_rerank.py first)", file=sys.stderr)
        sys.exit(1)
    base_pools = json.loads(BASE_POOL.read_text())

    conn = psycopg.connect(DSN, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SELECT id, source_id, query, expected_code FROM kg.eval_gold "
                    "WHERE persona='naive_vague' AND source_type='atar' ORDER BY id")
        gold = [dict(r) for r in cur.fetchall()]
    conn.close()
    if N_LIMIT:
        gold = gold[:N_LIMIT]
    print(f"HyDE on {len(gold)} naive_vague queries | model={MODEL} effort={EFFORT}")

    # 1. generate hypothetical docs (cached)
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=2, timeout=60.0)
    sem = asyncio.Semaphore(CONCURRENCY)
    cache = json.loads(HYDE_CACHE.read_text()) if HYDE_CACHE.exists() else {}
    todo = [g for g in gold if str(g["id"]) not in cache]
    if todo:
        print(f"  generating {len(todo)} hypothetical docs...")
        texts = await asyncio.gather(*[gen_hyde(client, sem, g["query"]) for g in todo])
        for g, t in zip(todo, texts):
            cache[str(g["id"])] = t
        HYDE_CACHE.write_text(json.dumps(cache))
    else:
        print("  [hyde cache hit]")

    # 2. retrieve on each hypothetical doc (LOO), union with cached base pool
    print("  retrieving on hypothetical docs + unioning...")
    base_ranks, union_hits = [], {k: 0 for k in K_LIST}
    base_hits = {k: 0 for k in K_LIST}
    t0 = time.time()
    for i, g in enumerate(gold):
        rec = base_pools[str(g["id"])]
        base_pool = rec["pool"]
        gold_code = g["expected_code"]
        br = first_rank(base_pool, gold_code)
        base_ranks.append(br)
        for k in K_LIST:
            if br is not None and br <= k:
                base_hits[k] += 1
        hyde_text = cache.get(str(g["id"])) or ""
        hyde_pool = []
        if hyde_text:
            fe, ee = _loo_exclusions(g.get("source_id"))
            try:
                hyde_pool = local_db.retrieve_candidates(
                    hyde_text, limit=POOL, exclude_fact_sources=fe, exclude_edge_ids=ee, **RETRIEVE_KWARGS)
            except Exception as exc:
                print(f"  [hyde retrieve error {g['id']}: {exc!r}]", flush=True)
        for k in K_LIST:
            if union_recall(base_pool, hyde_pool, gold_code, k):
                union_hits[k] += 1
        if (i + 1) % 20 == 0:
            print(f"    {i + 1}/{len(gold)}  ({time.time() - t0:.0f}s)", flush=True)

    n = len(gold)
    print(f"\n{'K':>5}  {'base':>8}  {'base+HyDE':>10}  {'delta':>7}")
    for k in K_LIST:
        b, u = base_hits[k] / n, union_hits[k] / n
        print(f"{k:>5}  {b:>8.3f}  {u:>10.3f}  {u - b:>+7.3f}")


if __name__ == "__main__":
    asyncio.run(main())
