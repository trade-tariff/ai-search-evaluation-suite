#!/usr/bin/env python3
"""Apply the deployable-app KG scope profile to a restored tariff DB.

Default mode is a dry run. Pass --apply after reviewing the counts.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable

import psycopg


APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parents[1]
PRODUCT_APP_ROOT = Path(
    os.environ.get("PRODUCT_APP_ROOT")
    or REPO_ROOT / "apps" / "product"
).resolve()
PRODUCT_BACKEND = PRODUCT_APP_ROOT / "backend"
if str(PRODUCT_BACKEND) not in sys.path:
    sys.path.insert(0, str(PRODUCT_BACKEND))

os.environ.setdefault("AI_FAN_OUT_KG_LABEL_PROFILE", "deployable")

from classification_core.evidence_labels import (  # noqa: E402
    DEPLOYABLE_CONSUMER_USE_SCOPES,
    DEPLOYABLE_EVIDENCE_ROLES,
    DEPLOYABLE_USE_SCOPES,
    active_evidence_role_definitions,
    active_use_scope_definitions,
)


def _csv(values: Iterable[str]) -> str:
    return ",".join(values)


def _count(cur: psycopg.Cursor, sql: str, params: tuple = ()) -> int:
    cur.execute(sql, params)
    value = cur.fetchone()[0]
    return int(value or 0)


def _filter_table(
    cur: psycopg.Cursor,
    *,
    schema: str,
    table: str,
    consumer_scopes: list[str],
    use_scopes: list[str],
    evidence_roles: list[str],
    apply: bool,
) -> dict[str, int]:
    fq = f"{schema}.{table}"
    before = _count(cur, f"SELECT count(*) FROM {fq}")
    would_delete = _count(
        cur,
        f"SELECT count(*) FROM {fq} WHERE NOT (use_scopes && %s::text[])",
        (consumer_scopes,),
    )
    would_normalize_scopes = _count(
        cur,
        f"""
        SELECT count(*)
        FROM {fq}
        WHERE use_scopes && %s::text[]
          AND NOT (use_scopes <@ %s::text[])
        """,
        (consumer_scopes, use_scopes),
    )
    would_normalize_roles = _count(
        cur,
        f"""
        SELECT count(*)
        FROM {fq}
        WHERE use_scopes && %s::text[]
          AND NOT (evidence_roles <@ %s::text[])
        """,
        (consumer_scopes, evidence_roles),
    )
    if not apply:
        return {
            "before": before,
            "would_delete": would_delete,
            "deleted": 0,
            "would_normalize_scopes": would_normalize_scopes,
            "normalized_scopes": 0,
            "would_normalize_roles": would_normalize_roles,
            "normalized_roles": 0,
            "after": before - would_delete,
        }
    cur.execute(
        f"DELETE FROM {fq} WHERE NOT (use_scopes && %s::text[])",
        (consumer_scopes,),
    )
    deleted = cur.rowcount
    cur.execute(
        f"""
        UPDATE {fq}
        SET use_scopes = ARRAY(
              SELECT DISTINCT scope
              FROM unnest(use_scopes) AS scope
              WHERE scope = ANY(%s::text[])
            )
        WHERE NOT (use_scopes <@ %s::text[])
        """,
        (use_scopes, use_scopes),
    )
    normalized_scopes = cur.rowcount
    cur.execute(
        f"""
        UPDATE {fq}
        SET evidence_roles = COALESCE(
              NULLIF(
                ARRAY(
                  SELECT DISTINCT role
                  FROM unnest(evidence_roles) AS role
                  WHERE role = ANY(%s::text[])
                ),
                ARRAY[]::text[]
              ),
              ARRAY['unknown']::text[]
            )
        WHERE NOT (evidence_roles <@ %s::text[])
        """,
        (evidence_roles, evidence_roles),
    )
    normalized_roles = cur.rowcount
    after = _count(cur, f"SELECT count(*) FROM {fq}")
    return {
        "before": before,
        "would_delete": would_delete,
        "deleted": int(deleted or 0),
        "would_normalize_scopes": would_normalize_scopes,
        "normalized_scopes": int(normalized_scopes or 0),
        "would_normalize_roles": would_normalize_roles,
        "normalized_roles": int(normalized_roles or 0),
        "after": after,
    }


def _sync_label_definitions(
    cur: psycopg.Cursor,
    *,
    schema: str,
    use_scope_defs: dict[str, tuple[str, str]],
    evidence_role_defs: dict[str, tuple[str, str]],
    apply: bool,
) -> dict[str, int]:
    would_delete = _count(
        cur,
        f"""
        SELECT count(*)
        FROM {schema}.evidence_label_definitions
        WHERE (label_kind = 'use_scope' AND NOT (key = ANY(%s::text[])))
           OR (label_kind = 'evidence_role' AND NOT (key = ANY(%s::text[])))
        """,
        (list(use_scope_defs), list(evidence_role_defs)),
    )
    if not apply:
        return {"would_delete": would_delete, "deleted": 0, "upserted": len(use_scope_defs) + len(evidence_role_defs)}
    cur.execute(
        f"""
        DELETE FROM {schema}.evidence_label_definitions
        WHERE (label_kind = 'use_scope' AND NOT (key = ANY(%s::text[])))
           OR (label_kind = 'evidence_role' AND NOT (key = ANY(%s::text[])))
        """,
        (list(use_scope_defs), list(evidence_role_defs)),
    )
    deleted = int(cur.rowcount or 0)
    upserted = 0
    for key, (label, description) in use_scope_defs.items():
        cur.execute(
            f"""
            INSERT INTO {schema}.evidence_label_definitions (label_kind, key, label, description)
            VALUES ('use_scope', %s, %s, %s)
            ON CONFLICT (label_kind, key) DO UPDATE
            SET label = EXCLUDED.label, description = EXCLUDED.description
            """,
            (key, label, description),
        )
        upserted += 1
    for key, (label, description) in evidence_role_defs.items():
        cur.execute(
            f"""
            INSERT INTO {schema}.evidence_label_definitions (label_kind, key, label, description)
            VALUES ('evidence_role', %s, %s, %s)
            ON CONFLICT (label_kind, key) DO UPDATE
            SET label = EXCLUDED.label, description = EXCLUDED.description
            """,
            (key, label, description),
        )
        upserted += 1
    return {"would_delete": would_delete, "deleted": deleted, "upserted": upserted}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db"))
    parser.add_argument("--schema", default=os.environ.get("TARIFF_DB_KG_SCHEMA", "kg"))
    parser.add_argument("--apply", action="store_true", help="Commit changes. Without this, roll back after reporting.")
    args = parser.parse_args()

    consumer_scopes = list(DEPLOYABLE_CONSUMER_USE_SCOPES)
    use_scopes = list(DEPLOYABLE_USE_SCOPES)
    evidence_roles = list(DEPLOYABLE_EVIDENCE_ROLES)
    print(f"consumer_scopes={_csv(consumer_scopes)}")
    print(f"use_scopes={_csv(use_scopes)}")
    print(f"evidence_roles={_csv(evidence_roles)}")
    print(f"mode={'apply' if args.apply else 'dry-run'}")

    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            facet_stats = _filter_table(
                cur,
                schema=args.schema,
                table="commodity_facets",
                consumer_scopes=consumer_scopes,
                use_scopes=use_scopes,
                evidence_roles=evidence_roles,
                apply=args.apply,
            )
            edge_stats = _filter_table(
                cur,
                schema=args.schema,
                table="kg_edges",
                consumer_scopes=consumer_scopes,
                use_scopes=use_scopes,
                evidence_roles=evidence_roles,
                apply=args.apply,
            )
            labels = _sync_label_definitions(
                cur,
                schema=args.schema,
                use_scope_defs=active_use_scope_definitions(),
                evidence_role_defs=active_evidence_role_definitions(),
                apply=args.apply,
            )
            print(f"commodity_facets={facet_stats}")
            print(f"kg_edges={edge_stats}")
            print(f"evidence_label_definitions={labels}")
        if args.apply:
            conn.commit()
        else:
            conn.rollback()
    if not args.apply:
        print("dry-run only; rerun with --apply to commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
