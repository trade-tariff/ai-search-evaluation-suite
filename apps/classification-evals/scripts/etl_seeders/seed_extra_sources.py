"""Seed three additional KG sources: footnotes, measure conditions, search references.

All inserts carry proper authority_tier + provenance JSONB. Idempotent.

  - Footnotes (uk.footnotes + uk.footnote_descriptions + uk.footnote_association_goods_nomenclatures)
      Tier 7. KG edges. Body = the footnote text. Linked to specific CCs.

  - Measure conditions (uk.measure_conditions + uk.measure_condition_code_descriptions)
      Tier 3. Surfaces things like "requires certificate Y930" as facts on the
      affected commodity.

  - Search references (uk.search_references)
      Tier 4. Each curated alias becomes a `common_term` facet on the linked
      commodity, e.g. {slot: 'common_term', value: 'flip-flops'} for 6402200000.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
for p in (
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path(__file__).parent.parent / ".env",
):
    if p is not None and p.exists():
        load_dotenv(p)

import psycopg
from psycopg.rows import dict_row
try:
    from .evidence_labels import edge_labels, facet_labels
except ImportError:
    from evidence_labels import edge_labels, facet_labels

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")

# Limits for the POC (each chapter has many footnotes/conditions)
LIMIT_FOOTNOTES = int(os.environ.get("LIMIT_FOOTNOTES", "1000"))
LIMIT_CONDITIONS = int(os.environ.get("LIMIT_CONDITIONS", "15000"))
# POC slice chapters get prioritised so the cert-fact harvest reaches Ch 22/64/73 first.
SLICE_CHAPTERS = ("22", "64", "73")


def _flat(code: str) -> str:
    digits = re.sub(r"\D", "", code or "")
    return digits.ljust(10, "0")[:10] if digits else code


def _default_facet_use_scopes(source: str, key: str) -> list[str]:
    return facet_labels(source, key)[0]


def _default_facet_evidence_roles(source: str, key: str) -> list[str]:
    return facet_labels(source, key)[1]


def _default_edge_use_scopes(type_: str, authority_tier: int) -> list[str]:
    return edge_labels(type_, authority_tier)[0]


def _default_edge_evidence_roles(type_: str, authority_tier: int, **kwargs) -> list[str]:
    return edge_labels(type_, authority_tier, **kwargs)[1]


def with_actor(conn, actor: str):
    """Set the kg.actor session variable so audit log records who did it."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('kg.actor', %s, false)", (actor,))


# --- 1. Footnotes ----------------------------------------------------

def seed_footnotes(conn) -> tuple[int, int]:
    """Pull footnotes attached to active goods. Create one KG edge per
    (footnote, commodity) pair so the trader-side classifier can see them.
    """
    print("=" * 60)
    print("1. Footnotes -> KG edges (tier 7)")
    with_actor(conn, "seeder:footnotes")
    inserted_edges = 0
    inserted_links = 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT fagn.goods_nomenclature_item_id AS code,
                   fagn.footnote_type AS ft,
                   fagn.footnote_id AS fid,
                   fd.description
            FROM uk.footnote_association_goods_nomenclatures fagn
            JOIN uk.footnote_descriptions fd
              ON fd.footnote_id = fagn.footnote_id
             AND fd.footnote_type_id = fagn.footnote_type
            JOIN uk.goods_nomenclatures gn
              ON gn.goods_nomenclature_item_id = fagn.goods_nomenclature_item_id
            WHERE (fagn.validity_end_date IS NULL OR fagn.validity_end_date > now())
              AND gn.validity_end_date IS NULL
              AND fd.description IS NOT NULL
              AND length(fd.description) > 20
            ORDER BY fagn.goods_nomenclature_item_id
            LIMIT %s
            """,
            (LIMIT_FOOTNOTES,),
        )
        rows = cur.fetchall()
        print(f"  {len(rows)} (code, footnote) pairs to ingest")
        seen_edges: set[str] = set()
        for r in rows:
            code = _flat(r["code"])
            footnote_ref = f"{r['ft']}{r['fid']}"
            edge_id = f"footnote_{footnote_ref}"
            body = r["description"][:2000]
            if edge_id not in seen_edges:
                cur.execute(
                    """
                    INSERT INTO kg.kg_edges (id, type, scope, title, body, source, authority_tier, use_scopes, evidence_roles, provenance)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s::jsonb)
                    ON CONFLICT (id) DO UPDATE SET
                      body=EXCLUDED.body, authority_tier=EXCLUDED.authority_tier,
                      use_scopes=EXCLUDED.use_scopes,
                      evidence_roles=EXCLUDED.evidence_roles,
                      provenance=EXCLUDED.provenance
                    """,
                    (
                        edge_id, "footnote", "global",
                        f"Footnote {footnote_ref}", body,
                        f"UK Tariff Footnote {footnote_ref}",
                        7,
                        _default_edge_use_scopes("footnote", 7),
                        _default_edge_evidence_roles("footnote", 7, source=f"UK Tariff Footnote {footnote_ref}", edge_id=edge_id),
                        json.dumps({
                            "source_type": "footnote",
                            "footnote_type": r["ft"],
                            "footnote_id": r["fid"],
                        }),
                    ),
                )
                seen_edges.add(edge_id)
                inserted_edges += 1
            cur.execute(
                """
                INSERT INTO kg.kg_edge_commodities (edge_id, commodity_code)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                (edge_id, code),
            )
            inserted_links += 1
        conn.commit()
    print(f"  inserted edges: {inserted_edges}, links: {inserted_links}")
    return inserted_edges, inserted_links


# --- 2. Measure conditions ------------------------------------------

def seed_measure_conditions(conn) -> int:
    """Surface measure conditions (e.g. 'requires certificate Y930') as
    structured facts on the affected commodity.
    """
    print("=" * 60)
    print("2. Measure conditions -> facts (tier 3)")
    with_actor(conn, "seeder:measure_conditions")
    inserted = 0
    with conn.cursor() as cur:
        # Ensure the `requires_certificate` facet definition exists
        cur.execute(
            """
            INSERT INTO kg.facet_definitions (key, label, short_label)
            VALUES ('requires_certificate', 'Requires document or certificate', 'Doc cert')
            ON CONFLICT (key) DO NOTHING;
            INSERT INTO kg.facet_definitions (key, label, short_label)
            VALUES ('measure_condition_code', 'Measure condition code', 'Condition')
            ON CONFLICT (key) DO NOTHING
            """
        )
        # Sort slice chapters first so the LIMIT always covers Ch 22/64/73 even if
        # someone tightens it. Cert descriptions joined so retrieval FTS sees the
        # human-readable meaning, not the generic measure-condition boilerplate.
        cur.execute(
            f"""
            WITH latest_cert_desc AS (
                SELECT DISTINCT ON (certificate_type_code, certificate_code)
                       certificate_type_code, certificate_code, description
                FROM uk.certificate_descriptions
                ORDER BY certificate_type_code, certificate_code,
                         certificate_description_period_sid DESC
            ),
            raw AS (
                SELECT DISTINCT
                       m.goods_nomenclature_item_id AS code,
                       mc.condition_code,
                       mc.certificate_type_code,
                       mc.certificate_code,
                       mccd.description AS condition_desc,
                       lcd.description AS cert_desc
                FROM uk.measure_conditions mc
                JOIN uk.measures m ON m.measure_sid = mc.measure_sid
                LEFT JOIN uk.measure_condition_code_descriptions mccd
                  ON mccd.condition_code = mc.condition_code
                LEFT JOIN latest_cert_desc lcd
                  ON lcd.certificate_type_code = mc.certificate_type_code
                 AND lcd.certificate_code = mc.certificate_code
                WHERE (m.validity_end_date IS NULL OR m.validity_end_date > now())
                  AND mc.certificate_code IS NOT NULL
                  AND mc.certificate_code <> ''
            )
            SELECT * FROM raw
            ORDER BY CASE WHEN SUBSTRING(code, 1, 2) = ANY(%s) THEN 0 ELSE 1 END,
                     code, condition_code, certificate_type_code, certificate_code
            LIMIT %s
            """,
            (list(SLICE_CHAPTERS), LIMIT_CONDITIONS),
        )
        for r in cur.fetchall():
            code = _flat(r["code"])
            cert = f"{r['certificate_type_code']}{r['certificate_code']}" if r["certificate_type_code"] else r["certificate_code"]
            condition_desc = (r["condition_desc"] or r["condition_code"] or "")
            cert_desc = (r["cert_desc"] or "").strip()
            # Build a retrieval-friendly evidence string: cert meaning first, then
            # condition meaning. Both are useful for FTS/embedding matching.
            if cert_desc:
                evidence = f"{cert} - {cert_desc}. Condition {r['condition_code']}: {condition_desc}"[:500]
            else:
                evidence = f"{cert}. Condition {r['condition_code']}: {condition_desc}"[:500]
            cur.execute(
                """
                INSERT INTO kg.commodity_facets (commodity_code, facet_key, facet_value, source, confidence, evidence, authority_tier, use_scopes, evidence_roles, provenance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s::jsonb)
                ON CONFLICT (commodity_code, facet_key, facet_value, source) DO UPDATE
                  SET evidence = EXCLUDED.evidence,
                      use_scopes = EXCLUDED.use_scopes,
                      evidence_roles = EXCLUDED.evidence_roles,
                      provenance = EXCLUDED.provenance,
                      updated_at = now()
                """,
                (
                    code, "requires_certificate", cert,
                    "measure_condition", 1.0,
                    evidence,
                    3,
                    _default_facet_use_scopes("measure_condition", "requires_certificate"),
                    _default_facet_evidence_roles("measure_condition", "requires_certificate"),
                    json.dumps({
                        "source_type": "measure_condition",
                        "condition_code": r["condition_code"],
                        "certificate_type": r["certificate_type_code"],
                        "certificate_code": r["certificate_code"],
                        "cert_description": cert_desc,
                    }),
                ),
            )
            inserted += 1
        conn.commit()
    print(f"  inserted/updated condition-facts: {inserted}")
    return inserted


# --- 3. Search references --------------------------------------------

def seed_search_references(conn) -> int:
    """Each curated alias becomes a `common_term` facet on the linked CC."""
    print("=" * 60)
    print("3. Search references -> common_term facts (tier 4)")
    with_actor(conn, "seeder:search_references")
    inserted = 0
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kg.facet_definitions (key, label, short_label)
            VALUES ('common_term', 'Common trader term / alias', 'Alias')
            ON CONFLICT (key) DO NOTHING
            """
        )
        cur.execute(
            """
            SELECT title, goods_nomenclature_item_id AS code
            FROM uk.search_references
            WHERE referenced_class='Commodity'
              AND goods_nomenclature_item_id IS NOT NULL
              AND title IS NOT NULL
              AND length(title) BETWEEN 2 AND 80
            """
        )
        for r in cur.fetchall():
            code = _flat(r["code"])
            title = r["title"].strip()
            if not title:
                continue
            cur.execute(
                """
                INSERT INTO kg.commodity_facets (commodity_code, facet_key, facet_value, source, confidence, evidence, authority_tier, use_scopes, evidence_roles, provenance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s::jsonb)
                ON CONFLICT (commodity_code, facet_key, facet_value, source) DO NOTHING
                """,
                (
                    code, "common_term", title,
                    "search_reference", 1.0,
                    f"HMRC search reference: '{title}'",
                    4,
                    _default_facet_use_scopes("search_reference", "common_term"),
                    _default_facet_evidence_roles("search_reference", "common_term"),
                    json.dumps({
                        "source_type": "search_reference",
                        "alias": title,
                    }),
                ),
            )
            inserted += 1
        conn.commit()
    print(f"  inserted common_term facts: {inserted}")
    return inserted


# --- Driver ---------------------------------------------------------

def main():
    conn = psycopg.connect(DSN, row_factory=dict_row)
    seed_footnotes(conn)
    seed_measure_conditions(conn)
    seed_search_references(conn)
    # Summary
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM kg.commodity_facets")
        print(f"\nTotal facts after seed: {cur.fetchone()['n']}")
        cur.execute("SELECT count(*) AS n FROM kg.kg_edges")
        print(f"Total KG edges after seed: {cur.fetchone()['n']}")
        cur.execute("SELECT authority_tier, count(*) FROM kg.commodity_facets GROUP BY 1 ORDER BY 1")
        for r in cur.fetchall():
            print(f"  facets tier {r['authority_tier']}: {r['count']}")
        cur.execute("SELECT authority_tier, count(*) FROM kg.kg_edges GROUP BY 1 ORDER BY 1")
        for r in cur.fetchall():
            print(f"  edges tier {r['authority_tier']}: {r['count']}")


if __name__ == "__main__":
    main()
