"""Legacy mirror of AI labels into kg.commodity_facets.

Do not run this in the normal local demo path. `uk.goods_nomenclature_labels`
is already consumed by the labels leg and by `kg.composite_search_text`; copying
those aliases into `kg.commodity_facets` makes label vocabulary look like KG
facts and can inflate fact counts/retrieval weights.

Set ALLOW_LABEL_FACET_MIRROR=1 only for a deliberate backwards-compatibility
experiment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

for envp in (
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
    Path(__file__).parent.parent.parent / ".env",
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
):
    if envp is not None and envp.exists():
        load_dotenv(envp)
        break

import psycopg
from psycopg.rows import dict_row


DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
SOURCE = "goods_nomenclature_labels"


def _flat(code: str) -> str:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits.ljust(10, "0")[:10] if digits else str(code or "")


def _clean(value: object) -> str:
    text = str(value or "").strip()
    return " ".join(text.split())


def _terms(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = _clean(value)
        key = text.casefold()
        if 2 <= len(text) <= 100 and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def _insert_facet(
    cur,
    code: str,
    key: str,
    value: str,
    evidence: str,
    roles: list[str],
    provenance: dict,
) -> None:
    cur.execute(
        """
        INSERT INTO kg.commodity_facets
          (commodity_code, facet_key, facet_value, source, confidence, evidence,
           authority_tier, use_scopes, evidence_roles, provenance)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s::jsonb)
        ON CONFLICT (commodity_code, facet_key, facet_value, source) DO UPDATE SET
          evidence = EXCLUDED.evidence,
          use_scopes = EXCLUDED.use_scopes,
          evidence_roles = EXCLUDED.evidence_roles,
          provenance = EXCLUDED.provenance,
          updated_at = now()
        """,
        (
            code,
            key,
            value,
            SOURCE,
            0.9,
            evidence[:700],
            6,
            ["retrieval", "classification", "audit"],
            roles,
            json.dumps(provenance),
        ),
    )


def seed_labels(limit: int | None = None) -> int:
    conn = psycopg.connect(DSN, row_factory=dict_row)
    inserted = 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kg.facet_definitions (key, label, short_label)
                VALUES
                  ('label_description', 'AI label description', 'AI label'),
                  ('common_term', 'Common trader term / alias', 'Alias'),
                  ('known_brand', 'Known brand', 'Brand')
                ON CONFLICT (key) DO NOTHING
                """
            )
            cur.execute(
                """
                SELECT goods_nomenclature_item_id AS code, description,
                       original_description, synonyms, colloquial_terms, known_brands
                FROM uk.goods_nomenclature_labels
                WHERE expired = false
                  AND goods_nomenclature_item_id IS NOT NULL
                ORDER BY goods_nomenclature_item_id
                """ + (f" LIMIT {int(limit)}" if limit else "")
            )
            rows = cur.fetchall()
            for row in rows:
                code = _flat(row["code"])
                description = _clean(row.get("description"))
                original = _clean(row.get("original_description"))
                base_provenance = {
                    "source_type": "goods_nomenclature_labels",
                    "source_id": code,
                }
                if description:
                    _insert_facet(
                        cur,
                        code,
                        "label_description",
                        description,
                        f"AI label description: {description}",
                        ["product_identity", "index_text"],
                        {**base_provenance, "label_field": "description"},
                    )
                    inserted += 1
                for term in _terms(row.get("synonyms")) + _terms(row.get("colloquial_terms")):
                    evidence = f"AI label term for {code}: {term}"
                    _insert_facet(
                        cur,
                        code,
                        "common_term",
                        term,
                        evidence,
                        ["alias"],
                        {**base_provenance, "label_field": "synonyms_or_colloquial_terms"},
                    )
                    inserted += 1
                for brand in _terms(row.get("known_brands")):
                    evidence = f"AI label known brand for {code}: {brand}"
                    _insert_facet(
                        cur,
                        code,
                        "known_brand",
                        brand,
                        evidence,
                        ["alias"],
                        {**base_provenance, "label_field": "known_brands"},
                    )
                    inserted += 1
                if original and original != description:
                    _insert_facet(
                        cur,
                        code,
                        "label_description",
                        original,
                        f"Original tariff label text: {original}",
                        ["product_identity", "index_text"],
                        {**base_provenance, "label_field": "original_description"},
                    )
                    inserted += 1
            conn.commit()
        return inserted
    finally:
        conn.close()


def main() -> None:
    if os.environ.get("ALLOW_LABEL_FACET_MIRROR") != "1":
        raise SystemExit(
            "Refusing to mirror labels into kg.commodity_facets. "
            "Use uk.goods_nomenclature_labels / kg.composite_search_text instead, "
            "or set ALLOW_LABEL_FACET_MIRROR=1 for a legacy experiment."
        )
    limit = int(os.environ.get("LABEL_FACET_LIMIT", "0") or "0") or None
    count = seed_labels(limit=limit)
    print(f"label facets inserted/updated: {count}")


if __name__ == "__main__":
    main()
