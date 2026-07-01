"""SMOKE TEST for the strict (code-keyed) leave-one-out fix.

Background: the old per-row LOO only excluded the EXACT queried ATAR's facts/edge.
22 gold codes have MULTIPLE ATARs, so querying one left the sibling ATARs'
near-identical fingerprints in the index = leakage. Multi-ATAR codes scored
~14pp higher than single-ATAR codes as a result.

This runs ONE config (semantic + composite + facts + KG + triage, curated OFF,
strict LOO, active gold, full corpus) through the SAME run_config path the grid
uses, then compares recall@100 for:
  - multi-ATAR codes  (>1 distinct source_id in eval_gold for that expected_code)
  - single-ATAR codes (=1 distinct source_id)

GATE: if strict LOO works, the ~14pp gap must largely close (the two should be
~equal). If it does NOT close, the resolver is wrong - STOP, do not launch the
full re-run.

Usage:
  .venv/bin/python -m journey.smoke_loo
"""
from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

from . import run_eval

DSN = run_eval.DSN

# All 7 personas, full active corpus.
PERSONAS = ["naive_vague", "naive_branded", "naive_specific",
            "emu_generic", "emu_ordinary", "emu_specific", "original"]

# Composite + facts + KG + triage, curated OFF, vector ON, strict LOO.
SMOKE_CONFIG = dict(
    use_curated=False, use_vector=True,
    use_facts=True, use_facts_vec=True, facts_vec_cap=0.9,
    use_kg_context=True, use_kg_vec=True, kg_vec_cap=0.9,
    use_composite=True, triage=True, loo=True,
)


def _split_recall(conn, run_id: int, k: int = 100, active_filtered: bool = True) -> dict:
    """recall@k for multi-ATAR vs single-ATAR codes on one run.

    A multi-ATAR code = an expected_code with >1 distinct ATAR source_id in gold.
    The `active` filter on the multi-CTE is optional (column may not exist yet);
    it does not change which class a code falls in.
    """
    active_clause = "AND active" if active_filtered else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH multi AS (
              SELECT expected_code
              FROM kg.eval_gold
              WHERE source_id IS NOT NULL AND source_type = 'atar' {active_clause}
              GROUP BY expected_code
              HAVING count(DISTINCT source_id) > 1
            )
            SELECT
              CASE WHEN g.expected_code IN (SELECT expected_code FROM multi)
                   THEN 'multi' ELSE 'single' END AS klass,
              count(*) AS n_rows,
              count(DISTINCT g.expected_code) AS n_codes,
              avg((rr.rank_of_expected IS NOT NULL AND rr.rank_of_expected <= %s)::int) AS recall_at_k
            FROM kg.eval_run_results rr
            JOIN kg.eval_gold g ON g.id = rr.gold_id
            WHERE rr.run_id = %s
            GROUP BY 1
            """,
            (k, run_id),
        )
        return {r["klass"]: r for r in cur.fetchall()}


def main():
    k = int(os.environ.get("SMOKE_K", "100"))
    conn = psycopg.connect(DSN, row_factory=dict_row)

    # Load active gold for all personas (same loader logic as run_eval.main).
    # Tolerate the `active` column being absent (migration may be queued behind a
    # live eval run holding a lock) - the multi-vs-single gap analysis is valid
    # either way, since dedup never moves a row between the two classes.
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT id, query, expected_code, persona, source_id, source_type "
                "FROM kg.eval_gold WHERE active AND persona = ANY(%s) ORDER BY id",
                (PERSONAS,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            active_filtered = True
        except psycopg.errors.UndefinedColumn:
            conn.rollback()
            cur.execute(
                "SELECT id, query, expected_code, persona, source_id, source_type "
                "FROM kg.eval_gold WHERE persona = ANY(%s) ORDER BY id",
                (PERSONAS,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            active_filtered = False
    print(f"Gold rows: {len(rows)} (personas: {PERSONAS}; active-filtered={active_filtered})")

    loo_map = run_eval.build_loo_map(conn, rows)
    print(f"LOO map: {len(loo_map)} codes with ATAR fingerprints to exclude")

    run_id = run_eval.run_config(
        conn, "smoke_strict_loo", SMOKE_CONFIG, rows,
        loo_map=loo_map, personas=PERSONAS,
    )

    split = _split_recall(conn, run_id, k=k, active_filtered=active_filtered)
    multi = split.get("multi")
    single = split.get("single")
    rm = float(multi["recall_at_k"]) if multi else 0.0
    rs = float(single["recall_at_k"]) if single else 0.0
    gap_pp = (rm - rs) * 100.0

    print("\n" + "=" * 64)
    print(f"SMOKE-TEST RESULT (run_id={run_id}, strict LOO, recall@{k})")
    print("=" * 64)
    if multi:
        print(f"  multi-ATAR  codes={multi['n_codes']:>3} rows={multi['n_rows']:>4}  recall@{k}={rm:.4f}")
    if single:
        print(f"  single-ATAR codes={single['n_codes']:>3} rows={single['n_rows']:>4}  recall@{k}={rs:.4f}")
    print(f"  gap (multi - single) = {gap_pp:+.1f}pp   (was ~+14pp under broken LOO)")
    # Gate: gap should largely close. Treat <=4pp as "closed".
    verdict = "PASS - gap closed, strict LOO is working" if abs(gap_pp) <= 4.0 \
        else "FAIL - gap did NOT close; resolver is wrong, DO NOT launch full re-run"
    print(f"  VERDICT: {verdict}")
    print("=" * 64)

    conn.close()


if __name__ == "__main__":
    main()
