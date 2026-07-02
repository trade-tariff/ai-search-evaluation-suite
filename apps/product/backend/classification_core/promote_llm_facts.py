"""Promote validated LLM commodity facts into retrieval-facing KG facets.

The durable extraction output is kg.commodity_llm_facts. This script exposes those
facts to existing retrieval legs by upserting them into kg.commodity_facets with
retrieval/classification/qa scopes. Optional embedding uses text-embedding-3-small
and only runs when explicitly requested.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time

import psycopg
from psycopg.rows import dict_row

from .provider_guard import openai_allowed

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
KG_SCHEMA = os.environ.get("TARIFF_DB_KG_SCHEMA", "kg")

_FACT_KEYS = {
    "common_name": ("LLM common name", "Common name", 30, ["alias", "product_identity"]),
    "product_family": ("LLM product family", "Product family", 31, ["product_identity"]),
    "species_or_variety": ("LLM species or variety", "Species/variety", 32, ["product_identity"]),
    "material": ("LLM material", "Material", 33, ["material_composition"]),
    "composition": ("LLM composition", "Composition", 34, ["material_composition"]),
    "processing_state": ("LLM processing state", "Processing", 35, ["form_presentation"]),
    "form_or_presentation": ("LLM form or presentation", "Form", 36, ["form_presentation"]),
    "packaging": ("LLM packaging", "Packaging", 37, ["packaging_quantity", "form_presentation"]),
    "intended_use": ("LLM intended use", "Intended use", 38, ["function_use"]),
    "inclusion": ("LLM inclusion condition", "Inclusion", 39, ["legal_inclusion"]),
    "exclusion": ("LLM exclusion condition", "Exclusion", 40, ["legal_exclusion"]),
    "threshold_condition": ("LLM threshold condition", "Threshold", 41, ["composition_threshold", "legal_definition"]),
    "classification_rationale": ("LLM classification rationale", "Rationale", 42, ["classification_rationale"]),
    "regulatory_condition": ("LLM regulatory condition", "Regulatory", 43, ["measure_condition", "legal_definition"]),
}

_KEY_ALIASES = {
    "formorpresentation": "form_or_presentation",
    "presentation": "form_or_presentation",
    "form": "form_or_presentation",
    "use": "intended_use",
    "function": "intended_use",
    "commonname": "common_name",
    "productfamily": "product_family",
    "species": "species_or_variety",
    "speciesorvariety": "species_or_variety",
    "condition": "threshold_condition",
    "threshold": "threshold_condition",
    "rationale": "classification_rationale",
}


def _norm_key(raw: str | None) -> str:
    clean = re.sub(r"[^a-z0-9]+", "", (raw or "").lower())
    if clean in _KEY_ALIASES:
        return _KEY_ALIASES[clean]
    snake = re.sub(r"[^a-z0-9]+", "_", (raw or "").lower()).strip("_")
    return snake or "unknown"


def _facet_key(fact_key: str) -> str:
    return f"llm_{_norm_key(fact_key)}"


def _facet_meta(fact_key: str) -> tuple[str, str, int, list[str]]:
    key = _norm_key(fact_key)
    return _FACT_KEYS.get(key, (f"LLM {key.replace('_', ' ')}", key.replace("_", " ").title(), 60, ["index_text"]))


def _use_scopes(scope: str, qna_usefulness: float) -> list[str]:
    scopes = ["retrieval", "classification"]
    if scope == "qna" or qna_usefulness >= 0.65:
        scopes.append("qa")
    return scopes


def ensure_facet_definitions(conn) -> None:
    with conn.cursor() as cur:
        for fact_key in sorted(_FACT_KEYS):
            label, short_label, rank, _roles = _facet_meta(fact_key)
            cur.execute(
                f"""
                INSERT INTO {KG_SCHEMA}.facet_definitions
                  (key, label, short_label, value_set, applies_to_chapters, rank)
                VALUES (%s, %s, %s, '[]'::jsonb, ARRAY[]::text[], %s)
                ON CONFLICT (key) DO UPDATE
                  SET label = EXCLUDED.label,
                      short_label = EXCLUDED.short_label,
                      rank = LEAST({KG_SCHEMA}.facet_definitions.rank, EXCLUDED.rank)
                """,
                (_facet_key(fact_key), label, short_label, rank),
            )
    conn.commit()


def promote(conn, *, limit: int | None = None, min_confidence: float = 0.70) -> dict:
    ensure_facet_definitions(conn)
    params: list[object] = [min_confidence]
    limit_sql = ""
    if limit:
        limit_sql = " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, commodity_code, scope, fact_key, fact_value, label, source, evidence,
                   confidence, qna_usefulness, question_hint, model, prompt_version, run_id,
                   source_bundle_hash, raw_fact, quality, provenance
            FROM {KG_SCHEMA}.commodity_llm_facts
            WHERE status = 'active'
              AND confidence >= %s
              AND NULLIF(trim(fact_value), '') IS NOT NULL
            ORDER BY commodity_code, scope, fact_key, fact_value, id
            {limit_sql}
            """,
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    dynamic_keys = sorted({_norm_key(row.get("fact_key")) for row in rows})
    with conn.cursor() as cur:
        for fact_key in dynamic_keys:
            label, short_label, rank, _roles = _facet_meta(fact_key)
            cur.execute(
                f"""
                INSERT INTO {KG_SCHEMA}.facet_definitions
                  (key, label, short_label, value_set, applies_to_chapters, rank)
                VALUES (%s, %s, %s, '[]'::jsonb, ARRAY[]::text[], %s)
                ON CONFLICT (key) DO UPDATE
                  SET label = EXCLUDED.label,
                      short_label = EXCLUDED.short_label,
                      rank = LEAST({KG_SCHEMA}.facet_definitions.rank, EXCLUDED.rank)
                """,
                (_facet_key(fact_key), label, short_label, rank),
            )
    conn.commit()

    upserts = 0
    skipped = 0
    with conn.cursor() as cur:
        for row in rows:
            fact_key = _norm_key(row.get("fact_key"))
            value = " ".join(str(row.get("fact_value") or row.get("label") or "").split())[:500]
            evidence = " ".join(str(row.get("evidence") or "").split())[:1200]
            if not value or not evidence:
                skipped += 1
                continue
            _label, _short, _rank, roles = _facet_meta(fact_key)
            provenance = {
                "source_table": "kg.commodity_llm_facts",
                "llm_fact_id": row.get("id"),
                "llm_fact_source": row.get("source"),
                "scope": row.get("scope"),
                "model": row.get("model"),
                "prompt_version": row.get("prompt_version"),
                "run_id": row.get("run_id"),
                "source_bundle_hash": row.get("source_bundle_hash"),
                "qna_usefulness": row.get("qna_usefulness"),
                "question_hint": row.get("question_hint"),
                "raw_fact": row.get("raw_fact"),
                "quality": row.get("quality"),
                "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            scopes = _use_scopes(str(row.get("scope") or ""), float(row.get("qna_usefulness") or 0.0))
            cur.execute(
                f"""
                INSERT INTO {KG_SCHEMA}.commodity_facets
                  (commodity_code, facet_key, facet_value, source, confidence, evidence,
                   authority_tier, provenance, updated_at, embedding_stale, use_scopes, evidence_roles)
                VALUES (%s, %s, %s, %s, %s, %s, 6, %s::jsonb, now(), true, %s::text[], %s::text[])
                ON CONFLICT (commodity_code, facet_key, facet_value, source) DO UPDATE
                  SET confidence = GREATEST({KG_SCHEMA}.commodity_facets.confidence, EXCLUDED.confidence),
                      evidence = EXCLUDED.evidence,
                      authority_tier = EXCLUDED.authority_tier,
                      provenance = EXCLUDED.provenance,
                      updated_at = now(),
                      embedding_stale = ({KG_SCHEMA}.commodity_facets.embedding IS NULL),
                      use_scopes = EXCLUDED.use_scopes,
                      evidence_roles = EXCLUDED.evidence_roles
                """,
                (
                    row["commodity_code"],
                    _facet_key(fact_key),
                    value,
                    "kg.commodity_llm_facts",
                    float(row.get("confidence") or 0.0),
                    evidence,
                    json.dumps(provenance, default=str),
                    scopes,
                    roles,
                ),
            )
            upserts += 1
    conn.commit()
    return {"selected": len(rows), "upserted": upserts, "skipped": skipped}


def _embedding_text(row: dict) -> str:
    return " | ".join(
        part for part in [
            str(row.get("facet_key") or "").replace("llm_", ""),
            str(row.get("facet_value") or ""),
            str(row.get("evidence") or ""),
        ] if part
    )[:8000]


def embed_stale(conn, *, limit: int | None = None, batch_size: int = 64, model: str = "text-embedding-3-small") -> dict:
    if not openai_allowed():
        raise SystemExit("Provider calls are disabled. Set CLASSIFICATION_ALLOW_PROVIDER_CALLS=1 to embed facts.")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required to embed promoted facts.")
    from openai import OpenAI

    params: list[object] = []
    limit_sql = ""
    if limit:
        limit_sql = " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, facet_key, facet_value, evidence
            FROM {KG_SCHEMA}.commodity_facets
            WHERE source = 'kg.commodity_llm_facts'
              AND (embedding IS NULL OR embedding_stale)
            ORDER BY id
            {limit_sql}
            """,
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]
    if not rows:
        return {"embedded": 0, "selected": 0, "model": model}

    client = OpenAI(api_key=api_key, timeout=60)
    embedded = 0
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        texts = [_embedding_text(row) for row in batch]
        resp = client.embeddings.create(model=model, input=texts)
        with conn.cursor() as cur:
            for row, item in zip(batch, resp.data):
                literal = "[" + ",".join(f"{float(x):.8f}" for x in item.embedding) + "]"
                cur.execute(
                    f"""
                    UPDATE {KG_SCHEMA}.commodity_facets
                    SET embedding = %s::vector, embedding_stale = false, updated_at = now()
                    WHERE id = %s
                    """,
                    (literal, row["id"]),
                )
                embedded += 1
        conn.commit()
        print(json.dumps({"embedded": embedded, "selected": len(rows), "model": model}), flush=True)
    return {"embedded": embedded, "selected": len(rows), "model": model}


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote kg.commodity_llm_facts into retrieval-facing facets")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--min-confidence", type=float, default=0.70)
    parser.add_argument("--embed", action="store_true", help="Embed promoted stale facts using OpenAI embeddings")
    parser.add_argument("--embed-limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()

    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        result = promote(conn, limit=args.limit, min_confidence=args.min_confidence)
        if args.embed:
            result["embedding"] = embed_stale(conn, limit=args.embed_limit, batch_size=args.batch_size)
        else:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT count(*) AS stale
                    FROM {KG_SCHEMA}.commodity_facets
                    WHERE source = 'kg.commodity_llm_facts'
                      AND (embedding IS NULL OR embedding_stale)
                    """
                )
                result["stale_promoted_embeddings"] = int(cur.fetchone()["stale"] or 0)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
