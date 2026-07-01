"""Deep-pool rerank: does reranking a 500-deep candidate pool lift recall@100?

Motivation: on naive_vague LOO, desc_vec retrieval is recall@100=78.6% but
recall@500=90.0% (+both is 82.9 / 91.4). For ~8-9pp of queries the gold IS
retrieved - it just sits in ranks 101-500. A reranker's whole job is to pull
those into the top-100. Most direct lever on recall@100; NO training data.

Two phases (so rerank strategy can be iterated without re-retrieving):
  Phase 1  build_pools()  - retrieve top-POOL once per query (LOO), cache to disk
  Phase 2  rerank()       - LLM scores every candidate 0-10 (batched), re-sort

Robustness: every OpenAI call retries with backoff under a GLOBAL concurrency
cap; a batch that still fails falls back to RRF-proportional scores (never zeros
candidates, so a failed batch can't bury the gold).

Env: RERANK_POOL(500) RERANK_MODEL(gpt-5-mini) RERANK_BATCH(100)
     RERANK_N(0=all) RERANK_OAI_CONCURRENCY(6) REBUILD_POOL(0)
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
POOL = int(os.environ.get("RERANK_POOL", "500"))
MODEL = os.environ.get("RERANK_MODEL", "gpt-5-mini")
BATCH = int(os.environ.get("RERANK_BATCH", "100"))
N_LIMIT = int(os.environ.get("RERANK_N", "0"))
OAI_CONCURRENCY = int(os.environ.get("RERANK_OAI_CONCURRENCY", "6"))
QUERY_CONCURRENCY = int(os.environ.get("RERANK_QUERY_CONCURRENCY", "6"))
EFFORT = os.environ.get("RERANK_EFFORT", "low")
MODE = os.environ.get("RERANK_MODE", "resort")            # resort | rescue
RESCUE_KEEP = int(os.environ.get("RERANK_RESCUE_KEEP", "80"))
SCORE_DEPTH = int(os.environ.get("RERANK_SCORE_DEPTH", "200"))
RERANK_TARGET = 100
REBUILD = os.environ.get("REBUILD_POOL", "0") == "1"
K_LIST = [5, 10, 20, 50, 100]
DATA = Path(__file__).parent / "data"
POOL_CACHE = DATA / f"rerank_pool_nv_{POOL}.json"

# desc_vec retrieval (recall@500=90.0%); no multi_query - far faster, ~same ceiling.
RETRIEVE_KWARGS = dict(
    use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
    use_facts_vec=True, use_kg_vec=True, facts_vec_cap=0.9, kg_vec_cap=0.9,
)

_SYSTEM = """You rerank UK tariff commodity codes by how well each matches a trader's
product query. For EVERY numbered candidate output a relevance score 0-10:
  10 = almost certainly the correct commodity code for this product
   5 = plausible / same general area
   0 = irrelevant
Judge on product semantics, not string overlap. Return JSON only:
{"scores": {"<index>": <0-10 int>, ...}} covering every index shown."""


def _norm(code: str) -> str:
    return (code or "").replace(".", "")[:10]


def build_pools(gold: list[dict]) -> dict:
    if POOL_CACHE.exists() and not REBUILD:
        cached = json.loads(POOL_CACHE.read_text())
        if set(cached.keys()) >= {str(g["id"]) for g in gold}:
            print(f"  [pool cache hit] {POOL_CACHE.name}")
            return cached
    print(f"  building pools (retrieve top-{POOL} per query, LOO)...")
    pools: dict = {}
    t0 = time.time()
    for i, g in enumerate(gold):
        fe, ee = _loo_exclusions(g.get("source_id"))
        cands = local_db.retrieve_candidates(
            g["query"], limit=POOL, exclude_fact_sources=fe, exclude_edge_ids=ee, **RETRIEVE_KWARGS)
        pools[str(g["id"])] = {
            "query": g["query"], "source_id": g["source_id"], "expected_code": g["expected_code"],
            "pool": [{"commodity_code": c["commodity_code"], "description": (c.get("description") or "")[:120],
                      "score": float(c.get("score", 0.0))} for c in cands],
        }
        if (i + 1) % 10 == 0:
            print(f"    {i + 1}/{len(gold)}  ({time.time() - t0:.0f}s)")
    POOL_CACHE.write_text(json.dumps(pools))
    print(f"  pools cached -> {POOL_CACHE.name}  ({time.time() - t0:.0f}s)")
    return pools


async def _score_batch(client, sem, query, batch, offset, attempts=3) -> dict[int, float] | None:
    lines = [f"{offset + i}) {_norm(c['commodity_code'])}: {c.get('description', '')[:90]}"
             for i, c in enumerate(batch)]
    user = (f"## Trader query\n{query}\n\n## Candidates\n" + "\n".join(lines)
            + f"\n\nScore every index {offset}..{offset + len(batch) - 1}. JSON only.")
    for attempt in range(attempts):
        try:
            async with sem:
                # gpt-5 reasoning models default to heavy reasoning (~48s on a
                # 100-item scoring task); 'minimal' cuts that to ~10s. Scoring
                # relevance needs no deep reasoning.
                extra = {"reasoning_effort": EFFORT} if MODEL.startswith("gpt-5") else {}
                resp = await client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
                    response_format={"type": "json_object"},
                    **extra,
                )
            data = json.loads(resp.choices[0].message.content or "{}")
            out: dict[int, float] = {}
            for k, v in (data.get("scores") or {}).items():
                try:
                    out[int(k)] = float(v)
                except (ValueError, TypeError):
                    continue
            return out
        except Exception as exc:
            if attempt == attempts - 1:
                print(f"  [batch failed after {attempts}] {exc!r}")
                return None
            await asyncio.sleep(1.5 * (attempt + 1))
    return None


async def _score_all(client, sem, query, items: list[dict]) -> None:
    """Score items in batches, in place (sets _rr). RRF-proportional default so a
    failed batch keeps relative order rather than zeroing candidates."""
    n = len(items)
    for rk, c in enumerate(items):
        c["_rr"] = 5.0 * (1.0 - rk / max(n, 1))
    tasks = [_score_batch(client, sem, query, items[o:o + BATCH], o) for o in range(0, n, BATCH)]
    for o, res in zip(range(0, n, BATCH), await asyncio.gather(*tasks)):
        if not res:
            continue
        for li, score in res.items():
            if 0 <= li < n:
                items[li]["_rr"] = score


async def rerank(client, sem, query, cands: list[dict]) -> list[dict]:
    if MODE == "rescue":
        # Keep RRF top-KEEP untouched; the LLM re-sorts ranks KEEP..SCORE_DEPTH and the
        # top (TARGET-KEEP) fill the freed slots. NOTE: this is a TRADE, NOT safe - a gold
        # at base rank KEEP..TARGET can be DEMOTED out of the top-TARGET. Net effect on
        # recall@TARGET is empirical / persona-dependent (codex review). (Ranks <KEEP fixed.)
        keep = cands[:RESCUE_KEEP]
        scorable = cands[RESCUE_KEEP:SCORE_DEPTH]
        rest = cands[SCORE_DEPTH:]
        await _score_all(client, sem, query, scorable)
        scorable.sort(key=lambda c: (-c["_rr"], -c.get("score", 0.0)))
        fill = max(0, RERANK_TARGET - RESCUE_KEEP)
        return keep + scorable[:fill] + scorable[fill:] + rest
    # resort: score the whole pool, re-sort by LLM score (RRF tiebreak)
    await _score_all(client, sem, query, cands)
    return sorted(cands, key=lambda c: (-c["_rr"], -c.get("score", 0.0)))


def _first_rank(cands: list[dict], gold: str) -> int | None:
    g = _norm(gold)
    for idx, c in enumerate(cands, start=1):
        if _norm(c["commodity_code"]) == g:
            return idx
    return None


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr)
        sys.exit(1)
    DATA.mkdir(exist_ok=True)
    conn = psycopg.connect(DSN, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SELECT id, source_id, query, expected_code FROM kg.eval_gold "
                    "WHERE persona='naive_vague' AND source_type='atar' ORDER BY id")
        gold = [dict(r) for r in cur.fetchall()]
    conn.close()
    if N_LIMIT:
        gold = gold[:N_LIMIT]
    print(f"Reranking {len(gold)} naive_vague queries | pool={POOL} model={MODEL} batch={BATCH}")

    pools = build_pools(gold)
    # timeout per request so a hung socket can't deadlock the whole gather.
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=2, timeout=90.0)
    sem = asyncio.Semaphore(OAI_CONCURRENCY)
    qsem = asyncio.Semaphore(QUERY_CONCURRENCY)

    async def one(g):
        rec = pools[str(g["id"])]
        cands = [dict(c) for c in rec["pool"]]
        base = _first_rank(cands, rec["expected_code"])
        try:
            # Bound QUERY concurrency so later queries don't starve in the batch
            # queue; the wait_for clock then measures real compute, not queue wait.
            async with qsem:
                reranked = await asyncio.wait_for(rerank(client, sem, rec["query"], cands), timeout=240)
        except Exception as exc:
            print(f"  [rerank fallback {g['id']}: {exc!r}]", flush=True)
            reranked = cands
        return {"base": base, "rr": _first_rank(reranked, rec["expected_code"])}

    t0 = time.time()
    rows = await asyncio.gather(*[one(g) for g in gold])
    n = len(rows)
    print(f"  reranked {n} in {time.time() - t0:.0f}s")

    def recall(key, k):
        return sum(1 for r in rows if r[key] is not None and r[key] <= k) / n

    print(f"\n{'K':>5}  {'RRF baseline':>14}  {'reranked':>10}  {'delta':>7}")
    for k in K_LIST:
        b, rk = recall("base", k), recall("rr", k)
        print(f"{k:>5}  {b:>14.3f}  {rk:>10.3f}  {rk - b:>+7.3f}")
    in_pool = sum(1 for r in rows if r["base"] is not None) / n
    print(f"\ngold in pool (recall@{POOL}): {in_pool:.3f}  = rerank ceiling for recall@100")


if __name__ == "__main__":
    asyncio.run(main())
