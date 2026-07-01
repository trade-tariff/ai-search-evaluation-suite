"""Populate rescue_descvec + hyde_descvec recall@K across ALL personas,
CHECKPOINTED per persona: commits to kg.eval_runs.curve_json.per_persona after
each persona, and SKIPS personas already present (so a killed run resumes).

  rescue = keep RRF top-80, LLM promotes ranks 81-200 into the freed 20 slots.
  hyde   = generate a hypothetical tariff doc, retrieve on it, union with base.
Both build on a 500-deep desc_vec base pool (built once per query, shared).
LOO-honest. Designed to run under `caffeinate -i`.

Env: RH_MODEL(gpt-5-mini) RH_CONCURRENCY(6) RH_FORCE(0=skip done personas)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

for p in [
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row
from openai import AsyncOpenAI

from . import local_db
from .run_eval import _loo_exclusions

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MODEL = os.environ.get("RH_MODEL", "gpt-5-mini")
CONC = int(os.environ.get("RH_CONCURRENCY", "6"))
FORCE = os.environ.get("RH_FORCE", "0") == "1"
PERSONAS = ["naive_vague", "naive_branded", "naive_specific",
            "emu_generic", "emu_ordinary", "emu_specific", "original"]
POOL, KEEP, SCORE_DEPTH, TARGET = 500, 80, 200, 100
BATCH = 100
K_LIST = [5, 10, 20, 50, 100]
RETRIEVE_KWARGS = dict(use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
                       use_facts_vec=True, use_kg_vec=True, facts_vec_cap=0.9, kg_vec_cap=0.9)

_RESCUE_SYS = """You rerank UK tariff commodity codes by how well each matches a trader's
product query. For EVERY numbered candidate output a relevance score 0-10:
10 = almost certainly the correct code, 5 = plausible, 0 = irrelevant. Judge on
product semantics. Return JSON only: {"scores": {"<index>": <0-10 int>, ...}}."""
_HYDE_SYS = """You write a short hypothetical product description in formal UK/Harmonised
System tariff language to help retrieve the right commodity code. Given a trader's
casual description, write 2-3 sentences using tariff-style terms (material, function,
form, processing, use). Do not guess a code. Output the description text only."""


def _norm(c):
    return (c or "").replace(".", "")[:10]


def _first_rank(cands, gold):
    g = _norm(gold)
    for i, c in enumerate(cands, 1):
        if _norm(c["commodity_code"]) == g:
            return i
    return None


async def _chat(client, sem, system, user, effort, want_json):
    for attempt in range(3):
        try:
            async with sem:
                kw = {"model": MODEL, "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": user}]}
                if MODEL.startswith("gpt-5"):
                    kw["reasoning_effort"] = effort
                if want_json:
                    kw["response_format"] = {"type": "json_object"}
                resp = await client.chat.completions.create(**kw)
            return resp.choices[0].message.content or ""
        except Exception as exc:
            if attempt == 2:
                print(f"  [chat failed: {exc!r}]", flush=True)
                return ""
            await asyncio.sleep(1.5 * (attempt + 1))
    return ""


async def _rescue(client, sem, query, base):
    """Keep RRF top-KEEP; LLM scores ranks KEEP..SCORE_DEPTH; promote top (TARGET-KEEP)."""
    keep, scorable, rest = base[:KEEP], base[KEEP:SCORE_DEPTH], base[SCORE_DEPTH:]
    for rk, c in enumerate(scorable):
        c["_rr"] = 5.0 * (1.0 - rk / max(len(scorable), 1))
    for off in range(0, len(scorable), BATCH):
        chunk = scorable[off:off + BATCH]
        lines = [f"{off + i}) {_norm(c['commodity_code'])}: {(c.get('description') or '')[:90]}"
                 for i, c in enumerate(chunk)]
        user = f"## Query\n{query}\n\n## Candidates\n" + "\n".join(lines) + "\n\nScore every index. JSON only."
        out = await _chat(client, sem, _RESCUE_SYS, user, "low", True)
        try:
            for k, v in (json.loads(out or "{}").get("scores") or {}).items():
                idx = int(k)
                if 0 <= idx < len(scorable):
                    scorable[idx]["_rr"] = float(v)
        except Exception:
            pass
    scorable.sort(key=lambda c: (-c["_rr"], -c.get("score", 0.0)))
    fill = max(0, TARGET - KEEP)
    return keep + scorable[:fill] + scorable[fill:] + rest


def _union(base, hyde):
    best = {}
    for c in base + hyde:
        code = c["commodity_code"]
        best[code] = max(best.get(code, -1e9), float(c.get("score", 0.0)))
    return [{"commodity_code": code} for code, _ in sorted(best.items(), key=lambda kv: -kv[1])]


async def _one(client, sem, qsem, loop, g):
    async with qsem:
        fe, ee = _loo_exclusions(g.get("source_id"))
        base = await loop.run_in_executor(
            None, lambda: local_db.retrieve_candidates(
                g["query"], limit=POOL, exclude_fact_sources=fe, exclude_edge_ids=ee, **RETRIEVE_KWARGS))
        rescued = await _rescue(client, sem, g["query"], [dict(c) for c in base])
        hyde_text = await _chat(client, sem, _HYDE_SYS, f"Trader query: {g['query']}", "minimal", False)
        hyde_pool = []
        if hyde_text.strip():
            hyde_pool = await loop.run_in_executor(
                None, lambda: local_db.retrieve_candidates(
                    hyde_text, limit=POOL, exclude_fact_sources=fe, exclude_edge_ids=ee, **RETRIEVE_KWARGS))
        union = _union([dict(c) for c in base], hyde_pool)
        return {"rescue": _first_rank(rescued, g["expected_code"]),
                "hyde": _first_rank(union, g["expected_code"])}


def _curve(ranks, n):
    return {str(k): round(sum(1 for r in ranks if r is not None and r <= k) / n, 4) for k in K_LIST}


def _upsert(conn, label, persona, curve):
    with conn.cursor() as cur:
        cur.execute("SELECT curve_json FROM kg.eval_runs WHERE run_label=%s", (label,))
        row = cur.fetchone()
        cj = row["curve_json"] or {}
        cj.setdefault("per_persona", {})[persona] = curve
        cur.execute("UPDATE kg.eval_runs SET curve_json=%s::jsonb WHERE run_label=%s",
                    (json.dumps(cj), label))
    conn.commit()


async def main():
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr); sys.exit(1)
    conn = psycopg.connect(DSN, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SELECT curve_json->'per_persona' pp FROM kg.eval_runs WHERE run_label='rescue_descvec'")
        r = cur.fetchone()
        done = set((r["pp"] or {}).keys()) if r else set()
    todo_personas = PERSONAS if FORCE else [p for p in PERSONAS if p not in done]
    print(f"personas to do: {todo_personas} (already done: {sorted(done)})")

    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=2, timeout=90.0)
    sem = asyncio.Semaphore(CONC)
    qsem = asyncio.Semaphore(CONC)
    loop = asyncio.get_running_loop()

    for persona in todo_personas:
        with conn.cursor() as cur:
            cur.execute("SELECT id, source_id, query, expected_code FROM kg.eval_gold "
                        "WHERE persona=%s AND source_type='atar' ORDER BY id", (persona,))
            gold = [dict(x) for x in cur.fetchall()]
        t0 = time.time()
        results = await asyncio.gather(*[_one(client, sem, qsem, loop, g) for g in gold])
        n = len(results)
        rescue_curve = _curve([r["rescue"] for r in results], n)
        hyde_curve = _curve([r["hyde"] for r in results], n)
        _upsert(conn, "rescue_descvec", persona, rescue_curve)
        _upsert(conn, "hyde_descvec", persona, hyde_curve)
        print(f"  {persona}: n={n} rescue@100={rescue_curve['100']} hyde@100={hyde_curve['100']} "
              f"({time.time() - t0:.0f}s) [checkpointed]", flush=True)
    conn.close()
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
