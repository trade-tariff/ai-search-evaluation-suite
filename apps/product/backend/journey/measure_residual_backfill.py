"""Offline test of codex idea #1: residual sibling backfill (UNCAPPED, 100 + N variant).

Does NOT re-run retrieval - reads stored top_codes from a finished eval run, so it's
free + fast. Question it answers: on top of the AI-166 composite (current production),
does structurally injecting the 'Other'/residual SIBLINGS of the retrieved top-100
codes rescue any misses? i.e. is backfill different from / additive to AI-166?

Mechanism (per query): take top-100 retrieved codes; for each, find its siblings
(same hierarchy `path`); collect the residual ('Other'/N.E.S.) ones not already in the
top-100; if the gold code is among them, the miss is RESCUED. Recall@(100 + residuals).

Run: .venv/bin/python journey/measure_residual_backfill.py   (env: RUN_ID, PERSONA)
"""
import os
from collections import defaultdict

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
RUN_ID = int(os.environ.get("RUN_ID", "64"))            # 64 = ai_semantic_composite_triage (current prod)
PERSONA = os.environ.get("PERSONA", "naive_vague")
K = 100


def norm(c: str) -> str:
    d = "".join(ch for ch in (c or "") if ch.isdigit())
    return d.ljust(10, "0")[:10] if d else (c or "")


def is_residual(descr: str) -> bool:
    d = (descr or "").strip().lower()
    return d.startswith("other") or "not elsewhere specified" in d or "n.e.s" in d


def main():
    conn = psycopg.connect(DSN, row_factory=dict_row)
    cur = conn.cursor()

    # code -> (path, residual?) for current declarable (suffix 80) codes
    cur.execute("""
      SELECT DISTINCT ON (g.goods_nomenclature_item_id)
             g.goods_nomenclature_item_id AS code, g.path, d.description AS descr
      FROM uk.goods_nomenclatures g
      LEFT JOIN uk.goods_nomenclature_descriptions d
        ON d.goods_nomenclature_sid = g.goods_nomenclature_sid AND d.language_id = 'EN'
      WHERE g.validity_end_date IS NULL AND g.producline_suffix = '80'
      ORDER BY g.goods_nomenclature_item_id, d.oid DESC NULLS LAST
    """)
    meta = {}
    by_path = defaultdict(list)
    for r in cur.fetchall():
        code = norm(r["code"])
        meta[code] = {"path": tuple(r["path"] or []), "resid": is_residual(r["descr"])}
        by_path[meta[code]["path"]].append(code)

    cur.execute("""
      SELECT r.expected_code, r.top_codes, r.rank_of_expected
      FROM kg.eval_run_results r JOIN kg.eval_gold g ON g.id = r.gold_id
      WHERE r.run_id = %s AND g.persona = %s
    """, (RUN_ID, PERSONA))
    rows = cur.fetchall()
    n = len(rows)
    if n == 0:
        print(f"run {RUN_ID} persona {PERSONA}: NO stored per-query results. Pick a run with eval_run_results.")
        return

    base_hits = 0
    misses = []
    for r in rows:
        rank = r["rank_of_expected"]
        if rank is not None and rank <= K:
            base_hits += 1
        else:
            misses.append(r)

    resid_gold_misses = 0
    rescued = []
    backfill_sizes = []
    for r in misses:
        gold = norm(r["expected_code"])
        top100 = [norm(c) for c in (r["top_codes"] or [])[:K]]
        top100set = set(top100)
        backfill = set()
        for c in top100:
            cm = meta.get(c)
            if not cm:
                continue
            for sib in by_path.get(cm["path"], []):
                if meta[sib]["resid"] and sib not in top100set:
                    backfill.add(sib)
        backfill_sizes.append(len(backfill))
        if meta.get(gold, {}).get("resid"):
            resid_gold_misses += 1
        if gold in backfill:
            rescued.append(gold)

    avg_bf = sum(backfill_sizes) / len(backfill_sizes) if backfill_sizes else 0
    print(f"run {RUN_ID} ({PERSONA}): n={n}")
    print(f"base recall@{K}:              {base_hits}/{n} = {base_hits/n:.3f}")
    print(f"misses:                       {len(misses)}")
    print(f"  of which gold IS residual:  {resid_gold_misses}  ({resid_gold_misses/max(len(misses),1)*100:.0f}% of misses)")
    print(f"backfill candidates added:    avg {avg_bf:.1f} per query (the '+N')")
    print(f"RESCUED by backfill:          {len(rescued)}")
    print(f"new recall@({K}+N):            {(base_hits+len(rescued))}/{n} = {(base_hits+len(rescued))/n:.3f}  (+{len(rescued)/n*100:.1f}pp)")
    print(f"rescued golds: {rescued[:25]}")


if __name__ == "__main__":
    main()
