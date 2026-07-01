#!/usr/bin/env python3
"""Read-only journey-retrieval baseline over kg.eval_gold, scored identically to
bench_mcp.py (same stratified sample, same k, same digit-level recall). Runs
INSIDE the journey-app container (has the journey package + DB + OpenAI key).

Does NOT write the database. Prints a summary table to stdout.
Mirrors bench_mcp.py: 20 per persona, limit 20.
"""
import json, sys, time
from journey import local_db

PER_PERSONA = 20
LIMIT = 20
KS = [1, 5, 10, 20]
CONFIGS = {
    # all retrieval legs on (their strongest; NOTE: includes gold-derived facts/edges -> optimistic)
    "all_legs": dict(use_curated=True, use_labels=True, use_vector=True, use_facts=True,
                     use_kg_context=True, use_facts_vec=True, use_kg_vec=True),
    # pure description retrieval (FTS + description vector); no curated/labels/facts/kg.
    # This is the fair match to the MCP classification_search (no gold memorisation).
    "desc_only": dict(use_curated=False, use_labels=False, use_vector=True, use_facts=False,
                      use_kg_context=False, use_facts_vec=False, use_kg_vec=False),
}


def load_gold():
    with local_db._conn() as c, c.cursor() as cur:
        cur.execute("select persona, query, expected_code from kg.eval_gold "
                    "where active order by persona, id")
        rows = cur.fetchall()
    by = {}
    for r in rows:
        by.setdefault(r["persona"], []).append(r)
    sample = []
    for persona, rs in by.items():
        sample.extend(rs[:PER_PERSONA])
    return sample


def rank_of(expected, got, digits):
    e = expected[:digits]
    for i, c in enumerate(got):
        if str(c)[:digits] == e:
            return i + 1
    return None


def score(sample, cfg):
    results = []
    for g in sample:
        exp = str(g["expected_code"])
        try:
            cands = local_db.retrieve_candidates(g["query"], limit=LIMIT, **cfg)
            got = [str(x.get("commodity_code")) for x in cands if x.get("commodity_code")]
        except Exception as e:
            got = []
            print(f"  ERR {type(e).__name__}: {e}", file=sys.stderr)
        results.append({d: rank_of(exp, got, d) for d in (10, 8, 6, 4)} | {"persona": g["persona"]})
    return results


def agg(rows):
    n = len(rows)
    out = {"n": n}
    for d in (10, 8, 6, 4):
        for k in KS:
            out[f"r@{k}_d{d}"] = round(sum(1 for r in rows if r[d] and r[d] <= k) / n, 3)
    out["mrr_d8"] = round(sum(1.0 / r[8] for r in rows if r[8]) / n, 3)
    return out


def main():
    sample = load_gold()
    print(f"sample={len(sample)} ({PER_PERSONA}/persona) limit={LIMIT}")
    personas = sorted(set(r["persona"] for r in sample))
    for name, cfg in CONFIGS.items():
        t0 = time.time()
        res = score(sample, cfg)
        o = agg(res)
        print(f"\n=== journey retrieval [{name}]  ({time.time()-t0:.0f}s) ===")
        print(f"OVERALL  R@1(d10)={o['r@1_d10']}  R@10: d10={o['r@10_d10']} d8={o['r@10_d8']} "
              f"d6={o['r@10_d6']} d4={o['r@10_d4']}  R@20(d8)={o['r@20_d8']}  MRR(d8)={o['mrr_d8']}")
        print(f"{'persona':16}{'n':>4}{'R1d10':>7}{'R10d10':>7}{'R10d8':>7}{'R10d6':>7}{'R10d4':>7}")
        for p in personas:
            s = agg([r for r in res if r["persona"] == p])
            print(f"{p:16}{s['n']:>4}{s['r@1_d10']:>7}{s['r@10_d10']:>7}{s['r@10_d8']:>7}"
                  f"{s['r@10_d6']:>7}{s['r@10_d4']:>7}")


if __name__ == "__main__":
    main()
