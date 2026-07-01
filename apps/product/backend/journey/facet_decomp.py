"""Query-side FACET DECOMPOSITION for UK tariff retrieval (non-agentic variant
of the decompose-retrieve-fuse idea in arXiv 2605.14857).

Premise: a naive trader query ("steel bar", "metal rails") is vague and
multi-dimensional. A single retrieval call anchors on one phrasing and can miss
the gold when the deciding dimension (material vs form vs use) isn't the one the
query foregrounds. So we decompose the query into classification AXES and
retrieve per-axis, then fuse.

Pipeline (per query):
  1. ONE LLM call (gpt-5-mini, reasoning_effort=minimal, per-request timeout)
     -> JSON {material, form, function, features}. Empty axes allowed.
  2. retrieve_candidates once for the COMBINED original query, and once per
     NON-EMPTY axis (each a short tariff-style phrase).
  3. QUOTA fuse (NOT pure RRF): keep the top ~70 of the combined-query leg by
     its own rank (the trader's verbatim intent is privileged), then admit up to
     ~30 more from the per-axis legs, round-robin across axes, deduped -> a
     100-candidate set. Reserved slots stop a noisy axis from evicting the
     combined leg's head.

Offline eval (this module's __main__): naive_vague, LOO-honest, recall@100 of
facet-decomposed retrieval vs the baseline (combined-query retrieve_candidates
alone, identical kwargs). Reports cost ($/query) and latency. NO production
writes; decompositions are cached to data/facet_decomp_nv.json so re-runs are
free.

Env:
  FACET_MODEL(gpt-5-mini) FACET_EFFORT(minimal) FACET_CONCURRENCY(6)
  FACET_TIMEOUT(40) FACET_N(0=all) FACET_COMBINED_QUOTA(70) FACET_TOTAL(100)
  FACET_AXIS_LIMIT(100)

Run: .venv/bin/python -m journey.facet_decomp
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
MODEL = os.environ.get("FACET_MODEL", "gpt-5-mini")
EFFORT = os.environ.get("FACET_EFFORT", "minimal")
CONCURRENCY = int(os.environ.get("FACET_CONCURRENCY", "6"))
REQUEST_TIMEOUT = float(os.environ.get("FACET_TIMEOUT", "40"))
N_LIMIT = int(os.environ.get("FACET_N", "0"))

# Quota-fusion knobs.
TOTAL = int(os.environ.get("FACET_TOTAL", "100"))            # final candidate set size
COMBINED_QUOTA = int(os.environ.get("FACET_COMBINED_QUOTA", "70"))  # reserved head from combined leg
AXIS_LIMIT = int(os.environ.get("FACET_AXIS_LIMIT", "100"))  # per-leg retrieval depth

AXES = ["material", "form", "function", "features"]

DATA = Path(__file__).parent / "data"
DECOMP_CACHE = DATA / "facet_decomp_nv.json"

# gpt-5-mini token pricing ($/1M). Used only for the cost report.
PRICE_IN = float(os.environ.get("FACET_PRICE_IN", "0.25"))
PRICE_OUT = float(os.environ.get("FACET_PRICE_OUT", "2.00"))

# Same retrieval kwargs the established naive_vague experiments use (see
# measure_hyde.py). Baseline AND facet-decomp share these verbatim so the only
# variable is the axis fan-out + quota fusion.
RETRIEVE_KWARGS = dict(
    use_curated=False, use_vector=True, use_facts=True, use_kg_context=True,
    use_facts_vec=True, use_kg_vec=True, facts_vec_cap=0.9, kg_vec_cap=0.9,
)

_SYSTEM = """You decompose a trader's vague product description into the
classification AXES used to assign a UK / Harmonised System commodity code.

Return a SHORT tariff-style phrase (or empty string) for each axis:
- material: what it is made of (e.g. "stainless steel", "vulcanised rubber",
  "moulded plastic"). Empty if the query implies no material.
- form: its physical form / shape / processing (e.g. "bar, not further worked",
  "coiled wire", "moulded sheet", "tube fitting"). Empty if none implied.
- function: what it does or what it is used for (e.g. "structural support",
  "sealing joint", "children's toy"). Empty if none implied.
- features: distinguishing or qualifying features (e.g. "for industrial
  machinery", "decorative", "personalised", "non-electric"). Empty if none.

Rules:
- Each phrase is 1-6 words, in tariff vocabulary, expanding the trader's term
  with HS-style synonyms WITHOUT inventing facts the query rules out
  ("steel bar" -> material "steel/iron", NOT "aluminium").
- Leave an axis as "" (empty string) if the query gives no signal for it. Do not
  pad. A typical vague query fills 1-3 axes, not all 4.
- Do NOT guess a commodity code.

Output JSON only:
{"material": "...", "form": "...", "function": "...", "features": "..."}"""


def _norm(code: str) -> str:
    return (code or "").replace(".", "")[:10]


async def decompose(client: AsyncOpenAI, sem: asyncio.Semaphore, query: str) -> tuple[dict, dict]:
    """One LLM call -> {material, form, function, features} (empty axes allowed).

    Returns (axes_dict, usage_dict). Every OpenAI call carries a per-request
    timeout (REQUEST_TIMEOUT) so an unresponsive request can't deadlock the
    asyncio.gather. On any failure returns ({}, {}) and retrieval falls back to
    the combined query alone.
    """
    async with sem:
        for attempt in range(3):
            try:
                extra = {"reasoning_effort": EFFORT} if MODEL.startswith("gpt-5") else {}
                resp = await client.chat.completions.create(
                    model=MODEL,
                    response_format={"type": "json_object"},
                    messages=[{"role": "system", "content": _SYSTEM},
                              {"role": "user", "content": f"Trader query: {query.strip()}"}],
                    timeout=REQUEST_TIMEOUT,
                    **extra,
                )
                content = (resp.choices[0].message.content or "{}").strip()
                data = json.loads(content)
                axes = {a: str(data.get(a) or "").strip() for a in AXES}
                usage = {}
                if getattr(resp, "usage", None) is not None:
                    usage = {"in": resp.usage.prompt_tokens, "out": resp.usage.completion_tokens}
                return axes, usage
            except Exception as exc:
                if attempt == 2:
                    print(f"  [decompose failed: {exc!r}]", flush=True)
                    return {}, {}
                await asyncio.sleep(1.5 * (attempt + 1))
    return {}, {}


def _quota_fuse(
    combined: list[dict],
    axis_legs: dict[str, list[dict]],
    total: int = TOTAL,
    combined_quota: int = COMBINED_QUOTA,
) -> list[dict]:
    """Reserved-slot fusion (NOT pure RRF).

    1. Admit the top `combined_quota` of the combined-query leg, in its own rank
       order. The trader's verbatim phrasing is privileged - it keeps a
       guaranteed head that a noisy axis can't evict.
    2. Fill the remaining (total - admitted) slots from the per-axis legs,
       round-robin across axes by rank, deduping against what's admitted and
       across axes. This admits axis-only discoveries (the multi-dimensional
       products one phrasing misses) without letting any single axis dominate.

    Each surviving candidate is tagged with `surfaced_by` (which legs produced
    it) for inspection. Returns a list of <= total candidates.
    """
    admitted: dict[str, dict] = {}

    def _admit(c: dict, leg: str) -> None:
        code = c.get("commodity_code")
        if not code:
            return
        if code in admitted:
            sb = admitted[code]["surfaced_by"]
            if leg not in sb:
                sb.append(leg)
            return
        c2 = dict(c)
        c2["surfaced_by"] = [leg]
        admitted[code] = c2

    # 1. reserved head from the combined leg
    for c in combined[:combined_quota]:
        if len(admitted) >= total:
            break
        _admit(c, "combined")

    # 2. round-robin fill from the axis legs
    leg_names = [a for a in AXES if axis_legs.get(a)]
    cursors = {a: 0 for a in leg_names}
    while len(admitted) < total and leg_names:
        progressed = False
        for a in list(leg_names):
            leg = axis_legs[a]
            i = cursors[a]
            # advance past anything already admitted
            while i < len(leg) and leg[i].get("commodity_code") in admitted:
                i += 1
            cursors[a] = i
            if i >= len(leg):
                leg_names.remove(a)
                continue
            _admit(leg[i], a)
            cursors[a] = i + 1
            progressed = True
            if len(admitted) >= total:
                break
        if not progressed:
            break

    # Preserve admission order: combined head first (already rank-ordered), then
    # the round-robin axis admissions in the order they were added. dict in
    # py3.7+ keeps insertion order, which is exactly admission order.
    return list(admitted.values())[:total]


def facet_retrieve(
    query: str,
    axes: dict,
    *,
    total: int = TOTAL,
    combined_quota: int = COMBINED_QUOTA,
    axis_limit: int = AXIS_LIMIT,
    retrieve_kwargs: dict | None = None,
    exclude_fact_sources: list[str] | None = None,
    exclude_edge_ids: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """Run combined-query retrieval + per-non-empty-axis retrieval, then quota
    fuse to a `total`-candidate set.

    `axes` is the decompose() output ({} falls back to combined-only). All
    retrieval calls share `retrieve_kwargs` and the same LOO exclusions, so the
    comparison against the combined-only baseline is apples-to-apples.

    Returns (fused_candidates, debug) where debug carries the axis phrases and
    per-leg sizes for inspection. DB-only after the (already-made) LLM call.
    """
    rk = dict(retrieve_kwargs or RETRIEVE_KWARGS)
    rk["exclude_fact_sources"] = exclude_fact_sources
    rk["exclude_edge_ids"] = exclude_edge_ids

    combined = local_db.retrieve_candidates(query, limit=axis_limit, **rk)

    axis_legs: dict[str, list[dict]] = {}
    for a in AXES:
        phrase = (axes.get(a) or "").strip()
        if not phrase:
            continue
        try:
            axis_legs[a] = local_db.retrieve_candidates(phrase, limit=axis_limit, **rk)
        except Exception as exc:
            print(f"  [axis {a} retrieve error: {exc!r}]", flush=True)
            axis_legs[a] = []

    fused = _quota_fuse(combined, axis_legs, total=total, combined_quota=combined_quota)
    debug = {
        "axes": {a: axes.get(a, "") for a in AXES},
        "combined_size": len(combined),
        "axis_sizes": {a: len(v) for a, v in axis_legs.items()},
    }
    return fused, debug


def _rank_of(pool: list[dict], gold: str) -> int | None:
    g = _norm(gold)
    for idx, c in enumerate(pool, start=1):
        if _norm(c["commodity_code"]) == g:
            return idx
    return None


# Material / form-ambiguous slice the success bar calls out (steel bar, metal
# rails, ...): queries dominated by a material or form term, where the deciding
# axis is often NOT the one the trader led with.
_AMBIG_TOKENS = ("steel", "metal", "iron", "aluminium", "aluminum", "brass",
                 "copper", "bar", "rod", "rail", "wire", "tube", "pipe", "sheet",
                 "coil", "plate", "mesh")


def _is_ambiguous(query: str) -> bool:
    q = (query or "").lower()
    return any(tok in q for tok in _AMBIG_TOKENS)


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr)
        sys.exit(1)

    conn = psycopg.connect(DSN, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SELECT id, source_id, query, expected_code FROM kg.eval_gold "
                    "WHERE persona='naive_vague' ORDER BY id")
        gold = [dict(r) for r in cur.fetchall()]
    conn.close()
    if N_LIMIT:
        gold = gold[:N_LIMIT]
    print(f"Facet decomposition on {len(gold)} naive_vague queries | "
          f"model={MODEL} effort={EFFORT} concurrency={CONCURRENCY} timeout={REQUEST_TIMEOUT}s")
    print(f"  quota fusion: combined head={COMBINED_QUOTA} + axis fill -> {TOTAL} | axis_limit={AXIS_LIMIT}")

    # 1. decompose every query (cached, LLM-only). Each call carries a
    #    per-request timeout so gather can't deadlock.
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], max_retries=2, timeout=REQUEST_TIMEOUT)
    sem = asyncio.Semaphore(CONCURRENCY)
    cache = json.loads(DECOMP_CACHE.read_text()) if DECOMP_CACHE.exists() else {}
    todo = [g for g in gold if str(g["id"]) not in cache]
    tok_in = tok_out = 0
    if todo:
        print(f"  decomposing {len(todo)} queries...")
        t_llm = time.time()
        results = await asyncio.gather(*[decompose(client, sem, g["query"]) for g in todo])
        for g, (axes, usage) in zip(todo, results):
            cache[str(g["id"])] = axes
            tok_in += usage.get("in", 0)
            tok_out += usage.get("out", 0)
        DATA.mkdir(parents=True, exist_ok=True)
        DECOMP_CACHE.write_text(json.dumps(cache, indent=2))
        llm_wall = time.time() - t_llm
        print(f"  decomposed in {llm_wall:.1f}s  (tokens in={tok_in} out={tok_out})")
    else:
        llm_wall = 0.0
        print("  [decomp cache hit]")

    # 2. baseline (combined-only) vs facet-decomp (quota-fused), LOO-honest.
    print("  retrieving baseline + facet-decomp per query...")
    base_hit = facet_hit = 0
    base_hit_a = facet_hit_a = n_amb = 0
    base_ranks: list[int | None] = []
    facet_ranks: list[int | None] = []
    retrieval_secs = 0.0
    example = None

    for i, g in enumerate(gold):
        gold_code = g["expected_code"]
        fe, ee = _loo_exclusions(g.get("source_id"))
        axes = cache.get(str(g["id"]), {}) or {}

        rk = dict(RETRIEVE_KWARGS)
        rk["exclude_fact_sources"] = fe
        rk["exclude_edge_ids"] = ee

        t0 = time.time()
        # Baseline: combined query alone, top-TOTAL.
        base_pool = local_db.retrieve_candidates(g["query"], limit=TOTAL, **rk)
        # Facet-decomp: shares the SAME combined retrieval depth internally.
        facet_pool, dbg = facet_retrieve(
            g["query"], axes,
            exclude_fact_sources=fe, exclude_edge_ids=ee,
        )
        retrieval_secs += time.time() - t0

        br = _rank_of(base_pool, gold_code)
        fr = _rank_of(facet_pool, gold_code)
        base_ranks.append(br)
        facet_ranks.append(fr)
        b_ok = br is not None and br <= TOTAL
        f_ok = fr is not None and fr <= TOTAL
        base_hit += int(b_ok)
        facet_hit += int(f_ok)

        amb = _is_ambiguous(g["query"])
        if amb:
            n_amb += 1
            base_hit_a += int(b_ok)
            facet_hit_a += int(f_ok)

        # Capture a clean example: an ambiguous query the facet decomp RESCUES
        # (baseline miss -> facet hit), else fall back to first ambiguous query.
        if example is None and amb and f_ok and not b_ok:
            example = {"query": g["query"], "gold": gold_code, "axes": dbg["axes"],
                       "base_rank": br, "facet_rank": fr, "rescued": True,
                       "axis_sizes": dbg["axis_sizes"]}
        if (i + 1) % 25 == 0:
            print(f"    {i + 1}/{len(gold)}  ({retrieval_secs:.0f}s retrieval)", flush=True)

    if example is None:
        # No rescue - show the first ambiguous query so the report still has a
        # concrete decomposition.
        for g in gold:
            if _is_ambiguous(g["query"]):
                axes = cache.get(str(g["id"]), {}) or {}
                example = {"query": g["query"], "gold": g["expected_code"], "axes": axes,
                           "rescued": False}
                break

    n = len(gold)
    base_r = base_hit / n
    facet_r = facet_hit / n
    # Cost: amortise the one decomposition call per query over the run.
    cost_total = (tok_in / 1_000_000) * PRICE_IN + (tok_out / 1_000_000) * PRICE_OUT
    n_llm = len(todo) if todo else n
    cost_per_query = cost_total / n_llm if n_llm else 0.0
    # Latency: LLM decomposition wall (concurrent) amortised per query, plus the
    # marginal retrieval cost (facet fan-out adds the per-axis legs over base).
    llm_per_query = (llm_wall / n_llm) if (todo and n_llm) else 0.0

    print("\n" + "=" * 64)
    print("FACET DECOMPOSITION vs BASELINE  (naive_vague, LOO-honest, recall@100)")
    print("=" * 64)
    print(f"  n queries:                  {n}")
    print(f"  baseline recall@{TOTAL}:        {base_hit}/{n} = {base_r:.3f}")
    print(f"  facet-decomp recall@{TOTAL}:    {facet_hit}/{n} = {facet_r:.3f}")
    print(f"  DELTA:                      {facet_r - base_r:+.3f}  ({(facet_r - base_r) * 100:+.1f}pp)")
    if n_amb:
        ba, fa = base_hit_a / n_amb, facet_hit_a / n_amb
        print(f"\n  material/form-ambiguous slice (n={n_amb}):")
        print(f"    baseline recall@{TOTAL}:      {base_hit_a}/{n_amb} = {ba:.3f}")
        print(f"    facet-decomp recall@{TOTAL}:  {facet_hit_a}/{n_amb} = {fa:.3f}")
        print(f"    DELTA:                    {fa - ba:+.3f}  ({(fa - ba) * 100:+.1f}pp)")
    rescued = sum(1 for b, f in zip(base_ranks, facet_ranks)
                  if (b is None or b > TOTAL) and (f is not None and f <= TOTAL))
    regressed = sum(1 for b, f in zip(base_ranks, facet_ranks)
                    if (b is not None and b <= TOTAL) and (f is None or f > TOTAL))
    print(f"\n  rescued (base miss -> facet hit): {rescued}")
    print(f"  regressed (base hit -> facet miss): {regressed}")
    print(f"\n  cost: ${cost_per_query:.5f}/query  (1 decomposition call; "
          f"{tok_in + tok_out} tok over {n_llm} q)")
    print(f"  latency: ~{llm_per_query * 1000:.0f}ms LLM/query (amortised, conc={CONCURRENCY}) "
          f"+ {retrieval_secs / n * 1000:.0f}ms retrieval/query (base+axes)")

    if example:
        print("\n  example decomposition:")
        print(f"    query: {example['query']!r}  (gold {example['gold']})")
        for a in AXES:
            print(f"      {a:<9}: {example['axes'].get(a, '')!r}")
        if example.get("rescued"):
            print(f"    -> RESCUED: baseline rank {example['base_rank']} (miss), "
                  f"facet rank {example['facet_rank']} (hit)")
            print(f"    -> axis leg sizes: {example.get('axis_sizes')}")


if __name__ == "__main__":
    asyncio.run(main())
