"""Gold-validity gate for data/commodity_validation_set.json.

Every row's gold code must be usable as a classification target against the
CURRENT uk schema, i.e. the code must:
  1. exist in goods_nomenclatures with producline_suffix '80',
  2. be inside its validity window today, and
  3. be a declarable leaf - no live child at a deeper indent in tree order
     (the same test CDS applies before accepting a code on a declaration).

Rows failing any check make accuracy numbers computed from this set
meaningless (an unwinnable gold is scored as a model miss). The sentinel row
9999999999 (retrieval-miss probe) is deliberately not a real code and is
skipped with a note.

NOTE: the goods_nomenclatures.path column is NOT used for the leaf test -
~2.5k live rows carry a NULL path (e.g. every leaf under 2208 30), so the
indent-sequence test (next live tree row's indent <= own indent) is the
reliable one.

Exit codes: 0 = all rows pass, 1 = one or more rows fail,
2 = gate could not run (file/DB problem - inconclusive, NOT a pass).

Usage:
  python3 validate_gold.py [path/to/commodity_validation_set.json]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get(
    "TARIFF_DB_DSN",
    "postgresql:///tariff_db",
)
SCHEMA = os.environ.get("TARIFF_DB_SCHEMA", "uk")

SENTINEL_CODE = "9999999999"  # deliberate retrieval-miss probe, never a real code

DEFAULT_SET_PATH = Path(__file__).resolve().parents[2] / "data" / "commodity_validation_set.json"

# One row per distinct gold code: best (live-first, then latest) suffix-80
# generation, its current indent, and the next live tree row's indent. A code
# is a declarable leaf iff the next live row in (item_id, suffix) order does
# not sit at a deeper indent.
GATE_SQL = f"""
WITH golds AS (
  SELECT unnest(%(codes)s::text[]) AS code
),
target AS (
  SELECT DISTINCT ON (g.code)
         g.code,
         n.goods_nomenclature_sid AS sid,
         n.validity_start_date::date AS vstart,
         n.validity_end_date::date AS vend,
         (n.validity_start_date::date <= current_date
          AND (n.validity_end_date IS NULL OR n.validity_end_date::date >= current_date)) AS live
  FROM golds g
  JOIN {SCHEMA}.goods_nomenclatures n
    ON n.goods_nomenclature_item_id = g.code AND n.producline_suffix = '80'
  ORDER BY g.code,
           (n.validity_start_date::date <= current_date
            AND (n.validity_end_date IS NULL OR n.validity_end_date::date >= current_date)) DESC,
           n.validity_start_date DESC, n.oid DESC
)
SELECT g.code, t.sid, t.vstart, t.vend, t.live,
       own.number_indents AS own_indent,
       nxt.item_id AS next_code, nxt.sfx AS next_suffix, nxt.number_indents AS next_indent
FROM golds g
LEFT JOIN target t ON t.code = g.code
LEFT JOIN LATERAL (
  SELECT i.number_indents
  FROM {SCHEMA}.goods_nomenclature_indents i
  WHERE i.goods_nomenclature_sid = t.sid
    AND i.validity_start_date::date <= current_date
  ORDER BY i.validity_start_date DESC, i.oid DESC
  LIMIT 1
) own ON t.live
LEFT JOIN LATERAL (
  SELECT n2.goods_nomenclature_item_id AS item_id, n2.producline_suffix AS sfx,
         i2.number_indents
  FROM {SCHEMA}.goods_nomenclatures n2
  JOIN LATERAL (
    SELECT number_indents
    FROM {SCHEMA}.goods_nomenclature_indents i2
    WHERE i2.goods_nomenclature_sid = n2.goods_nomenclature_sid
      AND i2.validity_start_date::date <= current_date
    ORDER BY i2.validity_start_date DESC, i2.oid DESC
    LIMIT 1
  ) i2 ON true
  WHERE (n2.goods_nomenclature_item_id, n2.producline_suffix) > (g.code, '80')
    AND n2.validity_start_date::date <= current_date
    AND (n2.validity_end_date IS NULL OR n2.validity_end_date::date >= current_date)
  ORDER BY n2.goods_nomenclature_item_id, n2.producline_suffix
  LIMIT 1
) nxt ON t.live
ORDER BY g.code
"""


def _failure_reason(code: str, row: dict) -> str | None:
    """Return None if the code passes the gate, else a human-readable reason."""
    if row["sid"] is None:
        return f"not found in {SCHEMA}.goods_nomenclatures (suffix 80)"
    if not row["live"]:
        if row["vend"] is not None:
            return f"expired: validity window {row['vstart']} .. {row['vend']}"
        return f"not yet valid: starts {row['vstart']}"
    if code[2:] == "00000000":
        # Chapter rows sit at indent 0, as do their headings, so the indent
        # sequence test is blind here - a chapter is never declarable.
        return "chapter-level code - never declarable"
    if row["own_indent"] is None:
        return "no indent record in goods_nomenclature_indents (data integrity)"
    if row["next_indent"] is not None and row["next_indent"] > row["own_indent"]:
        return (
            f"not a declarable leaf: live child {row['next_code']}/{row['next_suffix']} "
            f"at deeper indent ({row['next_indent']} > {row['own_indent']})"
        )
    return None


def validate(set_path: Path) -> int:
    try:
        payload = json.loads(set_path.read_text())
        rows = payload["rows"]
    except (OSError, json.JSONDecodeError, KeyError) as e:
        print(f"GATE INCONCLUSIVE: cannot read validation set {set_path}: {e}", file=sys.stderr)
        return 2

    checked = [r for r in rows if r.get("code") != SENTINEL_CODE]
    sentinels = len(rows) - len(checked)
    codes = sorted({r["code"] for r in checked if r.get("code")})

    try:
        with psycopg.connect(DSN, row_factory=dict_row) as c, c.cursor() as cur:
            cur.execute(GATE_SQL, {"codes": codes})
            by_code = {r["code"]: r for r in cur.fetchall()}
    except Exception as e:
        print(f"GATE INCONCLUSIVE: cannot query tariff_db ({DSN}): {e}", file=sys.stderr)
        return 2

    failures: list[tuple[str, str, str]] = []  # (code, first query, reason)
    for r in checked:
        code = r.get("code")
        if not code:
            failures.append(("<missing>", (r.get("queries") or [""])[0], "row has no 'code' field"))
            continue
        reason = _failure_reason(code, by_code[code])
        if reason:
            failures.append((code, (r.get("queries") or [""])[0], reason))

    print(f"Gold-validity gate: {len(checked)} rows checked against {SCHEMA} schema "
          f"(live + suffix 80 + declarable leaf)")
    if sentinels:
        print(f"Skipped {sentinels} sentinel row(s) {SENTINEL_CODE} - deliberate "
              f"retrieval-miss probe, exempt from the gate by design.")

    if failures:
        wq = max(len(q) for _, q, _ in failures)
        print(f"\n{'CODE':<12} {'FIRST QUERY':<{wq}}  REASON")
        for code, query, reason in failures:
            print(f"{code:<12} {query:<{wq}}  {reason}")
        print(f"\nGATE FAIL: {len(failures)} of {len(checked)} gold rows are invalid - "
              f"accuracy numbers from this set are untrustworthy until repaired.")
        return 1

    print(f"GATE PASS: all {len(checked)} gold rows are live declarable leaves.")
    return 0


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SET_PATH
    sys.exit(validate(path))
