"""Sync psycopg wrapper over the local tariff_db.

Powers retrieval (hybrid: curated search_references + FTS + pgvector + RRF)
and rate calculation (real measures, components, geographical memberships).
Same shape as the production InteractiveSearchService - if/when we want to
swap to the trade-tariff-backend HTTP API, only the SQL changes.
"""
from __future__ import annotations

import os
import re
import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from .evidence_labels import edge_labels, facet_labels
from .provider_guard import openai_allowed

DSN = os.environ.get(
    "TARIFF_DB_DSN",
    "postgresql:///tariff_db",
)
SCHEMA = os.environ.get("TARIFF_DB_SCHEMA", "uk")  # uk or xi - TARIC core (refreshed by prod dumps)
KG_SCHEMA = os.environ.get("TARIFF_DB_KG_SCHEMA", "kg")  # our knowledge layer - survives prod dumps


def _conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def _as_of_date(as_of: str | None = None) -> str:
    return as_of or date.today().isoformat()


@lru_cache(maxsize=32)
def _kg_has_column(table: str, column: str) -> bool:
    """Runtime-tolerant schema probe for optional KG migrations."""
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                  AND column_name = %s
                LIMIT 1
                """,
                (KG_SCHEMA, table, column),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _kg_use_scope_filter(alias: str, table: str, scope: str) -> str:
    if not _kg_has_column(table, "use_scopes"):
        return ""
    return f"AND '{scope}' = ANY({alias}.use_scopes)"


def health() -> dict:
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT count(*) AS total, count(search_embedding) AS embedded FROM {SCHEMA}.goods_nomenclature_self_texts")
            row = cur.fetchone()
            return {"ok": True, "schema": SCHEMA, **row}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# Was the odd one out: returned the ORIGINAL string when the input had no
# digits, where every other copy returned "". Now shared.
from commodity_codes import flat_code as _flat


def _dotted(code: str) -> str:
    digits = re.sub(r"\D", "", code or "")
    if len(digits) < 10:
        return code
    return f"{digits[0:4]}.{digits[4:6]}.{digits[6:8]}{digits[8:10] if digits[8:10] != '00' else ''}".rstrip(".")


# --- Retrieval (matches fan-out's hybrid shape) -------------------------

def _curated_leg(query: str, limit: int) -> list[dict]:
    """search_references is the HMRC-curated alias table (e.g. 'flip-flops' -> 6402200000).

    We also fan out to siblings of the matched code by joining on the
    first 4 digits (heading) so a curated hit broadens to its neighbours -
    this keeps Q&A flows useful.
    """
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH direct AS (
              SELECT sr.goods_nomenclature_item_id AS commodity_code,
                     COALESCE(
                       (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                        WHERE gnd.goods_nomenclature_sid = sr.goods_nomenclature_sid LIMIT 1),
                       sr.title
                     ) AS description,
                     similarity(sr.title, %s) AS score
              FROM {SCHEMA}.search_references sr
              WHERE sr.title %% %s OR sr.title ILIKE %s
              ORDER BY similarity(sr.title, %s) DESC
              LIMIT %s
            ),
            siblings AS (
              SELECT gn.goods_nomenclature_item_id AS commodity_code,
                     gnd.description AS description,
                     0.05 AS score
              FROM {SCHEMA}.goods_nomenclatures gn
              JOIN {SCHEMA}.goods_nomenclature_descriptions gnd
                ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
              WHERE gn.validity_end_date IS NULL
                AND LEFT(gn.goods_nomenclature_item_id, 4) IN (
                  SELECT DISTINCT LEFT(commodity_code, 4) FROM direct
                )
                AND gn.goods_nomenclature_item_id NOT IN (SELECT commodity_code FROM direct)
              LIMIT %s
            )
            SELECT * FROM direct
            UNION ALL
            SELECT * FROM siblings
            """,
            (query, query, f"%{query}%", query, limit, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "reference",
            }
            for r in cur.fetchall() if r["commodity_code"]
        ]


def _fts_leg(query: str, limit: int) -> list[dict]:
    """Full-text search across active goods nomenclature descriptions + self_texts."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT
              st.goods_nomenclature_item_id AS commodity_code,
              st.self_text AS description,
              ts_rank_cd(to_tsvector('english', st.self_text), q.tsq) AS score
            FROM {SCHEMA}.goods_nomenclature_self_texts st, q
            WHERE to_tsvector('english', st.self_text) @@ q.tsq
            ORDER BY score DESC
            LIMIT %s
            """,
            (query, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "fts",
            }
            for r in cur.fetchall()
        ]


def _description_substring_leg(query: str, limit: int) -> list[dict]:
    """Direct substring match against the active descriptions. Cheap fallback."""
    if not query.strip():
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (gn.goods_nomenclature_item_id)
                   gn.goods_nomenclature_item_id AS commodity_code,
                   gnd.description AS description,
                   similarity(gnd.description, %s) AS score
            FROM {SCHEMA}.goods_nomenclatures gn
            JOIN {SCHEMA}.goods_nomenclature_descriptions gnd
              ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
            WHERE gn.validity_end_date IS NULL
              AND gnd.description ILIKE %s
            ORDER BY gn.goods_nomenclature_item_id, similarity(gnd.description, %s) DESC
            LIMIT %s
            """,
            (query, f"%{query}%", query, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": r["description"] or "",
                "score": float(r["score"] or 0),
                "source": "substring",
            }
            for r in cur.fetchall()
        ]


# Tier multiplier for fact-retrieval - reflects how reliable the fact is as a
# representation of the commodity (not authority of the source).
_TIER_RETRIEVAL_WEIGHT = {
    1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0,  # all authoritative / curated
    5: 0.85,                          # LLM from ATAR description
    6: 0.70,                          # LLM from tariff description
    7: 0.70,                          # footnote / mixed
    8: 0.50,                          # external
}


def _fact_family_sql(alias: str = "cf") -> str:
    return (
        "CASE "
        f"WHEN {alias}.source LIKE 'atar:%%' THEN 'atar' "
        f"WHEN {alias}.source = 'search_reference' THEN 'search_references' "
        f"WHEN {alias}.source = 'description_llm' THEN 'description_llm' "
        f"WHEN {alias}.source = 'hand' THEN 'hand' "
        "ELSE 'other' END"
    )


def _fact_text_sql(alias: str, mode: str) -> str:
    expressions = {
        "full": (
            f"{alias}.facet_key || ' ' || {alias}.facet_value || ' ' || "
            f"COALESCE({alias}.evidence, '')"
        ),
        "applicant_raw": f"COALESCE({alias}.evidence, '')",
        "keywords": f"{alias}.facet_key || ' ' || {alias}.facet_value",
    }
    try:
        return expressions[mode]
    except KeyError as exc:
        raise ValueError(f"unknown fact_text_mode: {mode}") from exc


def _edge_family_sql(alias: str = "e") -> str:
    return (
        "CASE "
        f"WHEN {alias}.id LIKE 'atar\\_%%' THEN 'atar' "
        f"WHEN {alias}.id LIKE 'hsen:%%' OR {alias}.source LIKE 'hsen:%%' THEN 'hsen' "
        f"WHEN {alias}.source LIKE 'UK Tariff Chapter %% Notes' THEN 'chapter_notes' "
        f"WHEN {alias}.source LIKE 'UK Tariff Section %% Notes' THEN 'section_notes' "
        f"WHEN {alias}.type = 'classification_order' THEN 'girs' "
        f"WHEN {alias}.type = 'footnote' OR {alias}.source ILIKE '%%footnote%%' THEN 'footnotes' "
        "ELSE 'other' END"
    )


def _edge_text_sql(alias: str, mode: str) -> str:
    marker = r"HMRC (justification|classification rationale):"
    expressions = {
        "full": f"{alias}.title || ' ' || {alias}.body",
        "applicant_raw": (
            f"regexp_replace({alias}.body, '(?is)\\s*{marker}.*$', '')"
        ),
        "hmrc_justification": (
            f"regexp_replace({alias}.body, '(?is)^.*{marker}\\s*', '')"
        ),
    }
    try:
        return expressions[mode]
    except KeyError as exc:
        raise ValueError(f"unknown edge_text_mode: {mode}") from exc


def _facts_leg(
    query: str,
    limit: int,
    exclude_sources: Optional[list[str]] = None,
    include_families: Optional[list[str]] = None,
    exclude_families: Optional[list[str]] = None,
    text_mode: str = "full",
) -> list[dict]:
    """Retrieve codes whose structured facts have token overlap with the query.

    Builds a tsvector over (facet_key || ' ' || facet_value || ' ' || evidence)
    per fact and aggregates by commodity_code. Best tier wins per code.
    Returns commodity_code with a tier-weighted FTS rank.

    exclude_sources: list of fact-source values to exclude (e.g. ['atar:600014698']).
    Used by the leave-one-out eval to test for contamination - when scoring an
    ATAR's gold row, exclude its own derived facts so we measure generalisation
    rather than memorisation.
    """
    if not query.strip():
        return []
    excl = exclude_sources or []
    include = include_families or []
    exclude = exclude_families or []
    text_sql = _fact_text_sql("cf", text_mode)
    family_sql = _fact_family_sql("cf")
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT cf.commodity_code,
                   max(
                     ts_rank_cd(
                       to_tsvector('english', {text_sql}),
                       q.tsq
                     )
                     * COALESCE(
                       CASE cf.authority_tier
                         WHEN 1 THEN 1.0 WHEN 2 THEN 1.0 WHEN 3 THEN 1.0 WHEN 4 THEN 1.0
                         WHEN 5 THEN 0.85 WHEN 6 THEN 0.70 WHEN 7 THEN 0.70
                         ELSE 0.50
                       END, 0.70
                     )
                   ) AS score,
                   min(cf.authority_tier) AS best_tier,
                   (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                    JOIN {SCHEMA}.goods_nomenclatures gn
                      ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = cf.commodity_code
                      AND gn.validity_end_date IS NULL LIMIT 1) AS description
            FROM {KG_SCHEMA}.commodity_facets cf, q
            WHERE to_tsvector('english', {text_sql}) @@ q.tsq
              AND ( %s::text[] IS NULL OR cf.source <> ALL(%s::text[]) )
              AND ( %s::text[] IS NULL OR ({family_sql}) = ANY(%s::text[]) )
              AND ( %s::text[] IS NULL OR ({family_sql}) <> ALL(%s::text[]) )
              {_kg_use_scope_filter("cf", "commodity_facets", "retrieval")}
            GROUP BY cf.commodity_code
            ORDER BY score DESC
            LIMIT %s
            """,
            (
                query,
                excl or None,
                excl or None,
                include or None,
                include or None,
                exclude or None,
                exclude or None,
                limit,
            ),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "facts",
                "best_tier": r.get("best_tier"),
            }
            for r in cur.fetchall()
        ]


def _kg_context_leg(
    query: str,
    limit: int,
    exclude_edge_ids: Optional[list[str]] = None,
    include_families: Optional[list[str]] = None,
    exclude_families: Optional[list[str]] = None,
    text_mode: str = "full",
) -> list[dict]:
    """Retrieve codes via KG edges whose body matches the query.

    Captures the case where the trader's query language matches a chapter note,
    ATAR rationale, or footnote that's bound to specific commodities.

    exclude_edge_ids: skip these edges (used by LOO eval).
    """
    if not query.strip():
        return []
    excl = exclude_edge_ids or []
    include = include_families or []
    exclude = exclude_families or []
    text_sql = _edge_text_sql("e", text_mode)
    family_sql = _edge_family_sql("e")
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT kec.commodity_code,
                   max(
                     ts_rank_cd(
                       to_tsvector('english', {text_sql}),
                       q.tsq
                     )
                     * COALESCE(
                       CASE e.authority_tier
                         WHEN 1 THEN 1.0 WHEN 2 THEN 1.0 WHEN 3 THEN 1.0
                         ELSE 0.80
                       END, 0.80
                     )
                   ) AS score,
                   (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                    JOIN {SCHEMA}.goods_nomenclatures gn
                      ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = kec.commodity_code
                      AND gn.validity_end_date IS NULL LIMIT 1) AS description
            FROM {KG_SCHEMA}.kg_edges e
            JOIN {KG_SCHEMA}.kg_edge_commodities kec ON kec.edge_id = e.id
            CROSS JOIN q
            WHERE to_tsvector('english', {text_sql}) @@ q.tsq
              AND ( %s::text[] IS NULL OR e.id <> ALL(%s::text[]) )
              AND ( %s::text[] IS NULL OR ({family_sql}) = ANY(%s::text[]) )
              AND ( %s::text[] IS NULL OR ({family_sql}) <> ALL(%s::text[]) )
              {_kg_use_scope_filter("e", "kg_edges", "retrieval")}
            GROUP BY kec.commodity_code
            ORDER BY score DESC
            LIMIT %s
            """,
            (
                query,
                excl or None,
                excl or None,
                include or None,
                include or None,
                exclude or None,
                exclude or None,
                limit,
            ),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "kg_context",
            }
            for r in cur.fetchall()
        ]


def _vector_leg(query_embedding: list[float], limit: int) -> list[dict]:
    if not query_embedding:
        return []
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              st.goods_nomenclature_item_id AS commodity_code,
              st.self_text AS description,
              1 - (st.search_embedding <=> %s::vector) AS score
            FROM {SCHEMA}.goods_nomenclature_self_texts st
            WHERE st.search_embedding IS NOT NULL
            ORDER BY st.search_embedding <=> %s::vector
            LIMIT %s
            """,
            (literal, literal, limit),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "vector",
            }
            for r in cur.fetchall()
        ]


def _facts_vec_leg(
    query_embedding: list[float],
    limit: int,
    exclude_sources: Optional[list[str]] = None,
    include_families: Optional[list[str]] = None,
    exclude_families: Optional[list[str]] = None,
) -> list[dict]:
    """Semantic match against per-fact embeddings.

    Per codex's review: per-fact embeddings are more honest than per-code
    aggregates because they preserve which specific fact matched. Aggregation
    to commodity_code happens here via MAX(cosine) so a single highly-relevant
    fact lifts the code. Tier weighting still applies.

    Pre-filter with `embedding <-> q` first so the HNSW index is used; only
    aggregate the top candidates per fact.

    exclude_sources: skip facts with these source values (LOO eval).
    """
    if not query_embedding:
        return []
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    excl = exclude_sources or []
    include = include_families or []
    exclude = exclude_families or []
    family_sql = _fact_family_sql("cf")
    # Over-fetch facts so the per-commodity aggregation has plenty of choice
    # AND so the exclusion has plenty of fallback candidates.
    fact_pool = limit * 4
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH top_facts AS (
                SELECT cf.commodity_code,
                       cf.facet_key, cf.facet_value, cf.evidence, cf.authority_tier,
                       1 - (cf.embedding <=> %s::vector) AS cosine
                FROM {KG_SCHEMA}.commodity_facets cf
                WHERE cf.embedding IS NOT NULL
                  AND ( %s::text[] IS NULL OR cf.source <> ALL(%s::text[]) )
                  AND ( %s::text[] IS NULL OR ({family_sql}) = ANY(%s::text[]) )
                  AND ( %s::text[] IS NULL OR ({family_sql}) <> ALL(%s::text[]) )
                  {_kg_use_scope_filter("cf", "commodity_facets", "retrieval")}
                ORDER BY cf.embedding <=> %s::vector
                LIMIT %s
            )
            SELECT commodity_code,
                   max(
                     cosine *
                     CASE authority_tier
                       WHEN 1 THEN 1.0 WHEN 2 THEN 1.0 WHEN 3 THEN 1.0 WHEN 4 THEN 1.0
                       WHEN 5 THEN 0.85 WHEN 6 THEN 0.70 WHEN 7 THEN 0.70
                       ELSE 0.50
                     END
                   ) AS score,
                   min(authority_tier) AS best_tier,
                   (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                    JOIN {SCHEMA}.goods_nomenclatures gn
                      ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = commodity_code
                      AND gn.validity_end_date IS NULL LIMIT 1) AS description
            FROM top_facts
            GROUP BY commodity_code
            ORDER BY score DESC
            LIMIT %s
            """,
            (
                literal,
                excl or None,
                excl or None,
                include or None,
                include or None,
                exclude or None,
                exclude or None,
                literal,
                fact_pool,
                limit,
            ),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "facts_vec",
                "best_tier": r.get("best_tier"),
            }
            for r in cur.fetchall()
        ]


def _kg_vec_leg(
    query_embedding: list[float],
    limit: int,
    exclude_edge_ids: Optional[list[str]] = None,
    include_families: Optional[list[str]] = None,
    exclude_families: Optional[list[str]] = None,
) -> list[dict]:
    """Semantic match against per-edge embeddings, then join to commodities.

    Pre-filters edges with the HNSW index, then aggregates by linked commodity.

    exclude_edge_ids: skip these edges (LOO eval).
    """
    if not query_embedding:
        return []
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    excl = exclude_edge_ids or []
    include = include_families or []
    exclude = exclude_families or []
    family_sql = _edge_family_sql("e")
    edge_pool = limit * 4
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH top_edges AS (
                SELECT e.id, e.authority_tier,
                       1 - (e.embedding <=> %s::vector) AS cosine
                FROM {KG_SCHEMA}.kg_edges e
                WHERE e.embedding IS NOT NULL
                  AND ( %s::text[] IS NULL OR e.id <> ALL(%s::text[]) )
                  AND ( %s::text[] IS NULL OR ({family_sql}) = ANY(%s::text[]) )
                  AND ( %s::text[] IS NULL OR ({family_sql}) <> ALL(%s::text[]) )
                  {_kg_use_scope_filter("e", "kg_edges", "retrieval")}
                ORDER BY e.embedding <=> %s::vector
                LIMIT %s
            )
            SELECT kec.commodity_code,
                   max(
                     te.cosine *
                     CASE te.authority_tier
                       WHEN 1 THEN 1.0 WHEN 2 THEN 1.0 WHEN 3 THEN 1.0
                       ELSE 0.80
                     END
                   ) AS score,
                   (SELECT description FROM {SCHEMA}.goods_nomenclature_descriptions gnd
                    JOIN {SCHEMA}.goods_nomenclatures gn
                      ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = kec.commodity_code
                      AND gn.validity_end_date IS NULL LIMIT 1) AS description
            FROM top_edges te
            JOIN {KG_SCHEMA}.kg_edge_commodities kec ON kec.edge_id = te.id
            GROUP BY kec.commodity_code
            ORDER BY score DESC
            LIMIT %s
            """,
            (
                literal,
                excl or None,
                excl or None,
                include or None,
                include or None,
                exclude or None,
                exclude or None,
                literal,
                edge_pool,
                limit,
            ),
        )
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "kg_vec",
            }
            for r in cur.fetchall()
        ]


def _rrf_fuse(legs: list[list[dict]], limit: int, k: int = 60, leg_caps: Optional[list[float]] = None) -> list[dict]:
    """Reciprocal Rank Fusion across multiple ranked lists.

    leg_caps: optional multiplier per leg (e.g. 0.5 = leg's contribution to the
    fused score is halved). Use to cap secondary signals so they boost without
    dominating. Defaults to 1.0 per leg.
    """
    fused: dict[str, dict] = {}
    for i, leg in enumerate(legs):
        cap = (leg_caps[i] if leg_caps and i < len(leg_caps) else 1.0)
        for rank, r in enumerate(leg, start=1):
            code = r["commodity_code"]
            if not code:
                continue
            if code not in fused:
                fused[code] = {
                    "commodity_code": code,
                    "description": r["description"],
                    "score": 0.0,
                    "sources": [],
                }
            fused[code]["score"] += cap * 1.0 / (rank + k)
            if r.get("source") and r["source"] not in fused[code]["sources"]:
                fused[code]["sources"].append(r["source"])
    out = sorted(fused.values(), key=lambda x: -x["score"])
    return out[:limit]


def embed_query(text: str) -> Optional[list[float]]:
    """One OpenAI call to embed the query for the vector leg.

    Returns None if no API key or the call fails - retrieval gracefully
    falls back to keyword-only.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_allowed() or not api_key or not text.strip():
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        r = client.embeddings.create(model="text-embedding-3-small", input=text)
        return r.data[0].embedding
    except Exception as e:
        print(f"[embed] {type(e).__name__}: {e}")
        return None


def _composite_vector_leg(query_embedding: list[float], limit: int) -> list[dict]:
    """Vector leg over the production-style composite text (self_text + AI-166
    colloquial/synonyms/brands + references), per CompositeSearchTextBuilder."""
    if not query_embedding:
        return []
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT goods_nomenclature_item_id AS commodity_code,
                   composite_text AS description,
                   1 - (composite_embedding <=> %s::vector) AS score
            FROM {KG_SCHEMA}.composite_search_text
            WHERE composite_embedding IS NOT NULL
            ORDER BY composite_embedding <=> %s::vector
            LIMIT %s
            """,
            (literal, literal, limit),
        )
        return [{"commodity_code": r["commodity_code"], "description": (r["description"] or "")[:280],
                 "score": float(r["score"] or 0), "source": "vector_composite"} for r in cur.fetchall()]


def _composite_fts_leg(query: str, limit: int) -> list[dict]:
    """FTS over the composite text (includes AI-166 colloquial/synonyms/brands)."""
    if not query.strip():
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT goods_nomenclature_item_id AS commodity_code, composite_text AS description,
                   ts_rank_cd(to_tsvector('english', composite_text), q.tsq) AS score
            FROM {KG_SCHEMA}.composite_search_text, q
            WHERE to_tsvector('english', composite_text) @@ q.tsq
            ORDER BY score DESC LIMIT %s
            """,
            (query, limit),
        )
        return [{"commodity_code": r["commodity_code"], "description": (r["description"] or "")[:280],
                 "score": float(r["score"] or 0), "source": "fts_composite"} for r in cur.fetchall()]


def _live_retrieve_candidates(
    query: str,
    limit: int = 80,
    use_curated: bool = True,
    use_vector: bool = True,
    use_facts: bool = True,
    use_kg_context: bool = True,
    use_facts_vec: bool = True,
    use_kg_vec: bool = True,
    facts_cap: float = 0.5,
    kg_cap: float = 0.5,
    facts_vec_cap: float = 0.6,
    kg_vec_cap: float = 0.6,
    rrf_k: int = 60,
    exclude_fact_sources: Optional[list[str]] = None,
    exclude_edge_ids: Optional[list[str]] = None,
    include_fact_families: Optional[list[str]] = None,
    exclude_fact_families: Optional[list[str]] = None,
    include_edge_families: Optional[list[str]] = None,
    exclude_edge_families: Optional[list[str]] = None,
    fact_text_mode: str = "full",
    edge_text_mode: str = "full",
    query_embedding: Optional[list[float]] = None,
    use_vec_adapter: bool = False,
    use_composite: bool = False,
) -> list[dict]:
    """Hybrid retrieval. Same shape as fan-out's search.retrieve_candidates,
    plus optional facts + KG-context legs (lexical) and facts_vec + kg_vec
    (semantic, per codex's per-fact-embedding recommendation).

    Each leg returns a ranked list. RRF fuses with leg-specific caps so the
    secondary signals boost without dominating the primary description legs.
    Semantic caps default slightly higher than lexical caps because the
    embedding match is broader (catches synonyms / paraphrase) and otherwise
    contributes less per-rank than tight FTS matches.

    Embeds the query once and reuses for all three vector legs (description,
    facts, KG edges).
    """
    if not query.strip():
        return []
    legs: list[list[dict]] = []
    caps: list[float] = []
    if use_curated:
        try:
            legs.append(_curated_leg(query, limit))
            caps.append(1.0)
        except Exception as e:
            print(f"[retrieval curated] {e}")
    try:
        legs.append((_composite_fts_leg if use_composite else _fts_leg)(query, limit))
        caps.append(1.0)
    except Exception as e:
        print(f"[retrieval fts] {e}")
    try:
        legs.append(_description_substring_leg(query, limit))
        caps.append(1.0)
    except Exception as e:
        print(f"[retrieval substring] {e}")
    # Embed once, reuse for all vector legs.
    emb = query_embedding
    if emb is None and (use_vector or use_facts_vec or use_kg_vec):
        emb = embed_query(query)
    if use_vector and emb:
        try:
            # Adapter (Path A1) maps the QUERY vector toward code self-text
            # space; applied to the description leg only, not facts/kg vec.
            vec_emb = emb
            if use_vec_adapter:
                from . import adapter
                vec_emb = adapter.apply_adapter(emb)
            legs.append((_composite_vector_leg if use_composite else _vector_leg)(vec_emb, limit))
            caps.append(1.0)
        except Exception as e:
            print(f"[retrieval vector] {e}")
    if use_facts:
        try:
            legs.append(
                _facts_leg(
                    query,
                    limit,
                    exclude_sources=exclude_fact_sources,
                    include_families=include_fact_families,
                    exclude_families=exclude_fact_families,
                    text_mode=fact_text_mode,
                )
            )
            caps.append(facts_cap)
        except Exception as e:
            print(f"[retrieval facts] {e}")
    if use_kg_context:
        try:
            legs.append(
                _kg_context_leg(
                    query,
                    limit,
                    exclude_edge_ids=exclude_edge_ids,
                    include_families=include_edge_families,
                    exclude_families=exclude_edge_families,
                    text_mode=edge_text_mode,
                )
            )
            caps.append(kg_cap)
        except Exception as e:
            print(f"[retrieval kg_context] {e}")
    if use_facts_vec and emb:
        try:
            legs.append(
                _facts_vec_leg(
                    emb,
                    limit,
                    exclude_sources=exclude_fact_sources,
                    include_families=include_fact_families,
                    exclude_families=exclude_fact_families,
                )
            )
            caps.append(facts_vec_cap)
        except Exception as e:
            print(f"[retrieval facts_vec] {e}")
    if use_kg_vec and emb:
        try:
            legs.append(
                _kg_vec_leg(
                    emb,
                    limit,
                    exclude_edge_ids=exclude_edge_ids,
                    include_families=include_edge_families,
                    exclude_families=exclude_edge_families,
                )
            )
            caps.append(kg_vec_cap)
        except Exception as e:
            print(f"[retrieval kg_vec] {e}")
    return _rrf_fuse(legs, limit=limit, k=rrf_k, leg_caps=caps)


# --- Commodity metadata -------------------------------------------------

def commodity(code: str) -> Optional[dict]:
    flat = _flat(code)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT gn.goods_nomenclature_sid AS sid,
                   gn.goods_nomenclature_item_id AS code,
                   gnd.description AS description,
                   gn.validity_start_date,
                   gn.validity_end_date
            FROM {SCHEMA}.goods_nomenclatures gn
            JOIN {SCHEMA}.goods_nomenclature_descriptions gnd
              ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
            WHERE gn.goods_nomenclature_item_id = %s AND gn.validity_end_date IS NULL
            LIMIT 1
            """,
            (flat,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "sid": row["sid"],
            "code": row["code"],
            "code_dotted": _dotted(row["code"]),
            "description": row["description"] or "",
            "validity_start_date": row["validity_start_date"],
            "validity_end_date": row["validity_end_date"],
        }


# --- Geographical areas -------------------------------------------------

@lru_cache(maxsize=1)
def _country_descriptions() -> dict[str, str]:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT ga.geographical_area_id, gad.description
            FROM {SCHEMA}.geographical_areas ga
            JOIN {SCHEMA}.geographical_area_descriptions gad
              ON gad.geographical_area_sid = ga.geographical_area_sid
            WHERE ga.validity_end_date IS NULL
            """
        )
        return {r["geographical_area_id"]: r["description"] for r in cur.fetchall()}


def countries() -> list[dict]:
    """Active 2-letter country codes (skip groups, regions). Returns one row per country."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (ga.geographical_area_id)
                   ga.geographical_area_id AS code,
                   gad.description AS name,
                   ga.geographical_area_sid AS sid
            FROM {SCHEMA}.geographical_areas ga
            JOIN {SCHEMA}.geographical_area_descriptions gad
              ON gad.geographical_area_sid = ga.geographical_area_sid
            WHERE ga.validity_end_date IS NULL
              AND LENGTH(ga.geographical_area_id) = 2
            ORDER BY ga.geographical_area_id, gad.description
            """
        )
        return [{"code": r["code"], "name": r["name"], "sid": r["sid"]} for r in cur.fetchall()]


def country_groups(country_code: str, as_of: str | None = None) -> list[dict]:
    """All currently-active geographical groups the country belongs to."""
    as_of = _as_of_date(as_of)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT g.geographical_area_id AS code,
                   gd.description AS name,
                   g.geographical_area_sid AS sid
            FROM {SCHEMA}.geographical_area_memberships m
            JOIN {SCHEMA}.geographical_areas member
              ON member.geographical_area_sid = m.geographical_area_sid
            JOIN {SCHEMA}.geographical_areas g
              ON g.geographical_area_sid = m.geographical_area_group_sid
            JOIN {SCHEMA}.geographical_area_descriptions gd
              ON gd.geographical_area_sid = g.geographical_area_sid
            WHERE member.geographical_area_id = %s
              AND (m.validity_start_date IS NULL OR m.validity_start_date <= %s::date)
              AND (m.validity_end_date IS NULL OR m.validity_end_date > %s::date)
              AND (g.validity_start_date IS NULL OR g.validity_start_date <= %s::date)
              AND (g.validity_end_date IS NULL OR g.validity_end_date > %s::date)
            """,
            (country_code, as_of, as_of, as_of, as_of),
        )
        return [dict(r) for r in cur.fetchall()]


# --- Measures applicable to (commodity, country) -----------------------

# Measure types we care about
MFN_TYPE = "103"
PREFERENCE_TYPE = "142"
VAT_TYPES = ("305",)
SUPPLEMENTARY_TYPES = ("109", "110")
SUSPENSION_TYPE = "112"


def _parent_code_chain(code: str) -> list[str]:
    """Return the chain of progressively-broader parent codes for a 10-digit
    commodity, from itself UP to the heading.

    TARIC convention: a 10-digit code 7307110010 has parents that include
    its 8-digit (7307110000), 6-digit (7307110000 again, but stored as the
    subheading-padded form), and heading (7307000000). Trailing zeros mark
    the higher level. We enumerate the prefix-zero-padded forms at 8, 6, 4
    and let the INTERSECT with actually-existing codes happen in SQL.

    Codex's measure-inheritance fix lives on top of this: measures attached
    at a parent get unioned with measures on the exact code.
    """
    flat = _flat(code)
    if not flat or len(flat) != 10:
        return [flat] if flat else []
    chain = [flat]
    for n in (8, 6, 4):
        parent = flat[:n] + "0" * (10 - n)
        if parent != flat and parent not in chain:
            chain.append(parent)
    return chain


def applicable_measures(
    code: str,
    country_code: str,
    climb_hierarchy: bool = True,
    as_of: str | None = None,
) -> list[dict]:
    """Pull every measure that applies to (commodity, country) right now.

    Includes measures where geographical_area is the country itself, any of
    its parent groups, or ERGA OMNES (1011).

    climb_hierarchy=True (default): also include measures attached at any
    parent code in the TARIC hierarchy (heading / subheading). Per codex's
    review - measures inherit DOWN from heading-level definitions to all
    children unless explicitly overridden. Without this, rate calc and cert
    resolution miss the bulk of measures.
    """
    flat = _flat(code)
    as_of = _as_of_date(as_of)
    code_chain = _parent_code_chain(flat) if climb_hierarchy else [flat]

    groups = country_groups(country_code, as_of=as_of)
    geo_ids = [country_code, "1011"] + [g["code"] for g in groups]
    geo_ids = list(dict.fromkeys(geo_ids))  # dedupe, preserve order

    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              m.measure_sid,
              m.measure_type_id,
              mtd.description AS measure_type_description,
              m.geographical_area_id,
              gad.description AS geographical_area_description,
              m.goods_nomenclature_item_id,
              m.validity_start_date,
              m.validity_end_date,
              m.ordernumber,
              m.additional_code_id,
              (
                SELECT json_agg(json_build_object(
                  'rate_amount', mc.rate_amount,
                  'rate_expression_id', mc.rate_expression_id,
                  'monetary_unit_code', mc.monetary_unit_code,
                  'measurement_unit_code', mc.measurement_unit_code,
                  'measurement_unit_qualifier_code', mc.measurement_unit_qualifier_code
                ))
                FROM {SCHEMA}.measure_components mc WHERE mc.measure_sid = m.measure_sid
              ) AS components
            FROM {SCHEMA}.measures m
            LEFT JOIN {SCHEMA}.measure_type_descriptions mtd
              ON mtd.measure_type_id = m.measure_type_id
            LEFT JOIN {SCHEMA}.geographical_areas ga
              ON ga.geographical_area_id = m.geographical_area_id
              AND ga.validity_end_date IS NULL
            LEFT JOIN {SCHEMA}.geographical_area_descriptions gad
              ON gad.geographical_area_sid = ga.geographical_area_sid
            WHERE m.goods_nomenclature_item_id = ANY(%s)
              AND m.geographical_area_id = ANY(%s)
              AND (m.validity_end_date IS NULL OR m.validity_end_date > %s::date)
              AND m.validity_start_date <= %s::date
            ORDER BY m.measure_type_id, m.geographical_area_id, LENGTH(REPLACE(m.goods_nomenclature_item_id, '0', '')) DESC
            """,
            (code_chain, geo_ids, as_of, as_of),
        )
        out = []
        # Track (measure_type_id, geo) seen at narrower scope so a broader
        # measure attached at heading level doesn't shadow a more-specific
        # override on the exact code.
        seen: set[tuple] = set()
        for r in cur.fetchall():
            attached_code = r["goods_nomenclature_item_id"]
            key = (r["measure_type_id"], r["geographical_area_id"])
            inherited = attached_code != flat
            # Skip if a more-specific measure for the same (type, geo) already
            # claimed precedence (we order by specificity DESC so more-specific
            # comes first).
            if key in seen and inherited:
                continue
            seen.add(key)
            out.append({
                "measure_sid": r["measure_sid"],
                "measure_type_id": r["measure_type_id"],
                "measure_type_description": r["measure_type_description"] or "",
                "geographical_area_id": r["geographical_area_id"],
                "geographical_area_description": r["geographical_area_description"] or "",
                "components": r["components"] or [],
                "attached_at": attached_code,
                "inherited": inherited,
            })
        return out


def import_requirements(
    code: str,
    country_code: str,
    preference_claimed: str | None = None,
    as_of: str | None = None,
) -> dict:
    """Single deterministic resolver for "what does this scenario need".

    Per codex's review: handoff & rate should both consume one resolver
    that returns scenario-filtered measures (code + country + preference).

    Returns:
        {
            "code": <10-digit>,
            "country": <code>,
            "preference_claimed": <str|None>,
            "measures": [...],              # applicable measures with inheritance
            "rate_measures": [...],         # type 103/142 only (rate-bearing)
            "cert_documents": [...],        # from measure_conditions
            "supplementary_unit_measures": [...],  # type 109/110
            "vat_measures": [...],          # type 305
            "footnotes": [...],
        }
    """
    flat = _flat(code)
    measures = applicable_measures(flat, country_code, climb_hierarchy=True, as_of=as_of)

    rate_measures = [m for m in measures if m["measure_type_id"] in {"103", "142", "122"}]
    supp_measures = [m for m in measures if m["measure_type_id"] in {"109", "110", "111"}]
    vat_measures = [m for m in measures if m["measure_type_id"] in {"305", "306"}]

    cert_documents = _cert_documents_for_measures(measures, flat)

    return {
        "code": flat,
        "country": country_code,
        "preference_claimed": preference_claimed,
        "measures": measures,
        "rate_measures": rate_measures,
        "supplementary_unit_measures": supp_measures,
        "vat_measures": vat_measures,
        "cert_documents": cert_documents,
    }


def _cert_documents_for_chain(flat_code: str, as_of: str | None = None) -> list[dict]:
    """Live-resolve certificate documents from measure_conditions along the
    full parent chain. This is the deterministic version that handoff.py
    should call instead of the snapshot-via-facets reads.
    """
    chain = _parent_code_chain(flat_code)
    as_of = _as_of_date(as_of)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH latest_cert_desc AS (
                SELECT DISTINCT ON (certificate_type_code, certificate_code)
                       certificate_type_code, certificate_code, description
                FROM {SCHEMA}.certificate_descriptions
                ORDER BY certificate_type_code, certificate_code, certificate_description_period_sid DESC
            )
            SELECT DISTINCT
                   mc.certificate_type_code || COALESCE(mc.certificate_code, '') AS code,
                   lcd.description AS cert_description,
                   mc.condition_code,
                   mccd.description AS condition_desc,
                   m.goods_nomenclature_item_id AS attached_at
            FROM {SCHEMA}.measure_conditions mc
            JOIN {SCHEMA}.measures m ON m.measure_sid = mc.measure_sid
            LEFT JOIN latest_cert_desc lcd
              ON lcd.certificate_type_code = mc.certificate_type_code
             AND lcd.certificate_code = mc.certificate_code
            LEFT JOIN {SCHEMA}.measure_condition_code_descriptions mccd
              ON mccd.condition_code = mc.condition_code
            WHERE m.goods_nomenclature_item_id = ANY(%s)
              AND (m.validity_end_date IS NULL OR m.validity_end_date > %s::date)
              AND m.validity_start_date <= %s::date
              AND mc.certificate_code IS NOT NULL
              AND mc.certificate_code <> ''
            ORDER BY code
            """,
            (chain, as_of, as_of),
        )
        return [
            {
                "code": r["code"],
                "description": (r["cert_description"] or "").strip(),
                "condition_code": r["condition_code"],
                "condition_desc": (r["condition_desc"] or "").strip(),
                "attached_at": r["attached_at"],
                "inherited": r["attached_at"] != flat_code,
            }
            for r in cur.fetchall()
        ]


def _cert_documents_for_measures(measures: list[dict], flat_code: str) -> list[dict]:
    measure_sids = [m.get("measure_sid") for m in measures if m.get("measure_sid")]
    if not measure_sids:
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH latest_cert_desc AS (
                SELECT DISTINCT ON (certificate_type_code, certificate_code)
                       certificate_type_code, certificate_code, description
                FROM {SCHEMA}.certificate_descriptions
                ORDER BY certificate_type_code, certificate_code, certificate_description_period_sid DESC
            )
            SELECT DISTINCT
                   mc.certificate_type_code || COALESCE(mc.certificate_code, '') AS code,
                   lcd.description AS cert_description,
                   mc.condition_code,
                   mccd.description AS condition_desc,
                   m.goods_nomenclature_item_id AS attached_at
            FROM {SCHEMA}.measure_conditions mc
            JOIN {SCHEMA}.measures m ON m.measure_sid = mc.measure_sid
            LEFT JOIN latest_cert_desc lcd
              ON lcd.certificate_type_code = mc.certificate_type_code
             AND lcd.certificate_code = mc.certificate_code
            LEFT JOIN {SCHEMA}.measure_condition_code_descriptions mccd
              ON mccd.condition_code = mc.condition_code
            WHERE mc.measure_sid = ANY(%s)
              AND mc.certificate_code IS NOT NULL
              AND mc.certificate_code <> ''
            ORDER BY code
            """,
            (measure_sids,),
        )
        return [
            {
                "code": r["code"],
                "description": (r["cert_description"] or "").strip(),
                "condition_code": r["condition_code"],
                "condition_desc": (r["condition_desc"] or "").strip(),
                "attached_at": r["attached_at"],
                "inherited": r["attached_at"] != flat_code,
            }
            for r in cur.fetchall()
        ]


# --- Facets + KG edges (augmenting AI Guided Search) ------------------

def facets_for(code: str) -> list[dict]:
    """All structured facts on this commodity, multiple values per facet allowed."""
    flat = _flat(code)
    use_scopes_expr = (
        "cf.use_scopes"
        if _kg_has_column("commodity_facets", "use_scopes")
        else "NULL::text[] AS use_scopes"
    )
    evidence_roles_expr = (
        "cf.evidence_roles"
        if _kg_has_column("commodity_facets", "evidence_roles")
        else "NULL::text[] AS evidence_roles"
    )
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT cf.facet_key, cf.facet_value, cf.source, cf.confidence, cf.evidence,
                   cf.authority_tier, {use_scopes_expr}, {evidence_roles_expr}, cf.provenance,
                   fd.label AS facet_label
            FROM {KG_SCHEMA}.commodity_facets cf
            LEFT JOIN {KG_SCHEMA}.facet_definitions fd ON fd.key = cf.facet_key
            WHERE cf.commodity_code = %s
            ORDER BY fd.rank NULLS LAST, cf.facet_key, cf.confidence DESC
            """,
            (flat,),
        )
        return [dict(r) for r in cur.fetchall()]


def facets_for_codes(codes: list[str]) -> dict[str, list[dict]]:
    """Batch lookup. Returns {code: [facet, ...]}."""
    flats = [_flat(c) for c in codes]
    if not flats: return {}
    out: dict[str, list[dict]] = {f: [] for f in flats}
    use_scopes_expr = (
        "cf.use_scopes"
        if _kg_has_column("commodity_facets", "use_scopes")
        else "NULL::text[] AS use_scopes"
    )
    evidence_roles_expr = (
        "cf.evidence_roles"
        if _kg_has_column("commodity_facets", "evidence_roles")
        else "NULL::text[] AS evidence_roles"
    )
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT cf.commodity_code, cf.facet_key, cf.facet_value, cf.source, cf.confidence,
                   cf.evidence, cf.authority_tier, {use_scopes_expr}, {evidence_roles_expr}, cf.provenance,
                   fd.label AS facet_label, fd.rank
            FROM {KG_SCHEMA}.commodity_facets cf
            LEFT JOIN {KG_SCHEMA}.facet_definitions fd ON fd.key = cf.facet_key
            WHERE cf.commodity_code = ANY(%s)
            ORDER BY fd.rank NULLS LAST, cf.facet_key
            """,
            (flats,),
        )
        for r in cur.fetchall():
            out[r["commodity_code"]].append(dict(r))
    return out


def display_descriptions_for_codes(codes: list[str]) -> dict[str, str]:
    """Contextualized descriptions suitable for UI candidate cards."""
    flats = [_flat(c) for c in codes]
    if not flats:
        return {}
    out: dict[str, str] = {}
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT goods_nomenclature_item_id AS code, composite_text
            FROM {KG_SCHEMA}.composite_search_text
            WHERE goods_nomenclature_item_id = ANY(%s)
            """,
            (flats,),
        )
        for row in cur.fetchall():
            out[row["code"]] = _clean_contextual_description(row.get("composite_text") or "")
        missing = [f for f in flats if f not in out]
        if missing:
            cur.execute(
                f"""
                SELECT goods_nomenclature_item_id AS code, self_text
                FROM {SCHEMA}.goods_nomenclature_self_texts
                WHERE goods_nomenclature_item_id = ANY(%s)
                """,
                (missing,),
            )
            for row in cur.fetchall():
                out[row["code"]] = _clean_contextual_description(row.get("self_text") or "")
    return {code: desc for code, desc in out.items() if desc}


def _clean_contextual_description(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"^(?:commodity|code)\s+\d{8,10}\s*[:\-]\s*", "", text, flags=re.I)
    if len(text) > 260:
        cut = text[:257].rstrip(" ,.;")
        boundary = cut.rfind(" ")
        if boundary >= 170:
            cut = cut[:boundary].rstrip(" ,.;")
        text = cut + "..."
    return text


def kg_edges_for_candidates(codes: list[str], include: Optional[dict] = None) -> list[dict]:
    """Pull KG edges that apply to the candidate set - tightly scoped.

    Per codex's review: prior version pulled ALL section + ALL heading edges,
    regardless of whether they actually scoped to the candidates' section or
    heading. This drowned the prompt in irrelevant "hard" rules.

    Now: only include section/heading edges whose scope matches a section or
    heading the candidates actually belong to. Chapter scopes filtered to the
    actual candidate chapters (already was). Global scopes still always
    included (GIRs etc. are universal).

    Match via: (a) explicit kg_edge_commodities link, OR
    (b) edge scope is 'global', 'chapter:XX', 'heading:XXXX', 'section:N'
        for an XX/XXXX/N that the candidate set actually touches.
    """
    flats = [_flat(c) for c in codes]
    if not flats: return []
    inc = {
        "chapter_notes": True, "section_notes": True, "legacy_blob_notes": True,
        "girs": True, "atar_rationales": True, "heading_rules": True, "other_global": True,
        "hsen": True,
    }
    if include:
        inc.update(include)

    # Build the candidate scopes the edges should be limited to.
    chapters = sorted({f[:2] for f in flats})
    headings = sorted({f[:4] for f in flats})
    sections = sorted({_chapter_to_section(ch) for ch in chapters if _chapter_to_section(ch)})
    chapter_scopes = [f"chapter:{ch}" for ch in chapters]
    heading_scopes = [f"heading:{h}" for h in headings]
    section_scopes = [f"section:{s}" for s in sections]
    in_scope = ["global"] + chapter_scopes + heading_scopes + section_scopes

    with _conn() as c, c.cursor() as cur:
        use_scopes_expr = (
            "e.use_scopes"
            if _kg_has_column("kg_edges", "use_scopes")
            else "NULL::text[] AS use_scopes"
        )
        evidence_roles_expr = (
            "e.evidence_roles"
            if _kg_has_column("kg_edges", "evidence_roles")
            else "NULL::text[] AS evidence_roles"
        )
        cur.execute(
            f"""
            WITH explicit AS (
              SELECT DISTINCT e.id, e.type, e.scope, e.title, e.body, e.source, e.authority_tier, {use_scopes_expr}, {evidence_roles_expr}, e.provenance
              FROM {KG_SCHEMA}.kg_edges e
              JOIN {KG_SCHEMA}.kg_edge_commodities kec ON kec.edge_id = e.id
              WHERE kec.commodity_code = ANY(%s)
            ),
            by_scope AS (
              SELECT e.id, e.type, e.scope, e.title, e.body, e.source, e.authority_tier, {use_scopes_expr}, {evidence_roles_expr}, e.provenance
              FROM {KG_SCHEMA}.kg_edges e
              WHERE e.scope = ANY(%s)
                AND e.id NOT LIKE 'atar_%%'
            )
            SELECT * FROM explicit
            UNION
            SELECT * FROM by_scope
            ORDER BY scope, id
            """,
            (flats, in_scope),
        )
        all_edges = [dict(r) for r in cur.fetchall()]

    def _keep(e: dict) -> bool:
        eid = e["id"] or ""
        scope = e["scope"] or ""
        # HSEN family - WCO interpretive notes (tier 2)
        if eid.startswith("hsen:"):
            return inc.get("hsen", True)
        # Decomposed note family (per Note 1, 2, 3...)
        if "_note_" in eid:
            if scope.startswith("chapter:"):
                return inc["chapter_notes"]
            if scope.startswith("section:"):
                return inc["section_notes"]
        # Legacy blob notes (chXX_notes / secX_notes) - the non-decomposed blob
        if eid.endswith("_notes") and ("_note_" not in eid):
            return inc["legacy_blob_notes"]
        # GIRs
        if eid.startswith("gir_"):
            return inc["girs"]
        # ATAR rationales
        if eid.startswith("atar_"):
            return inc["atar_rationales"]
        # Heading-scoped rules
        if scope.startswith("heading:"):
            return inc["heading_rules"]
        # Other globals
        if scope == "global":
            return inc["other_global"]
        return True

    return [e for e in all_edges if _keep(e)]


# HS Section -> chapter range. Source: WCO HS structure. (Mirror of the table
# in seed_hsen.py so this module is self-contained.)
_SECTION_OF_CHAPTER: dict[str, str] = {}
for _section_num, _chs in {
    "I": range(1, 6), "II": range(6, 15), "III": [15], "IV": range(16, 25),
    "V": range(25, 28), "VI": range(28, 39), "VII": range(39, 41),
    "VIII": range(41, 44), "IX": range(44, 47), "X": range(47, 50),
    "XI": range(50, 64), "XII": range(64, 68), "XIII": range(68, 71),
    "XIV": [71], "XV": range(72, 84), "XVI": range(84, 86),
    "XVII": range(86, 90), "XVIII": range(90, 93), "XIX": [93],
    "XX": range(94, 97), "XXI": [97],
}.items():
    for _ch in _chs:
        _SECTION_OF_CHAPTER[f"{_ch:02d}"] = _section_num


def _chapter_to_section(ch: str) -> Optional[str]:
    return _SECTION_OF_CHAPTER.get(ch)


def facet_definitions(keys: Optional[list[str]] = None) -> list[dict]:
    with _conn() as c, c.cursor() as cur:
        if keys:
            cur.execute(
                f"SELECT key, label, short_label, value_set, applies_to_chapters, rank FROM {KG_SCHEMA}.facet_definitions WHERE key = ANY(%s) ORDER BY rank",
                (keys,),
            )
        else:
            cur.execute(
                f"SELECT key, label, short_label, value_set, applies_to_chapters, rank FROM {KG_SCHEMA}.facet_definitions ORDER BY rank",
            )
        return [dict(r) for r in cur.fetchall()]


def supplementary_unit_for(code: str, as_of: str | None = None) -> Optional[str]:
    """Find the supplementary unit code via type 109/110 measures."""
    flat = _flat(code)
    as_of = _as_of_date(as_of)
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT mc.measurement_unit_code
            FROM {SCHEMA}.measures m
            JOIN {SCHEMA}.measure_components mc ON mc.measure_sid = m.measure_sid
            WHERE m.goods_nomenclature_item_id = %s
              AND m.measure_type_id IN ('109','110')
              AND (m.validity_end_date IS NULL OR m.validity_end_date > %s::date)
              AND m.validity_start_date <= %s::date
            LIMIT 1
            """,
            (flat, as_of, as_of),
        )
        row = cur.fetchone()
        return row["measurement_unit_code"] if row else None


# --- Best applicable customs rate ---------------------------------------

def _component_rate(measure: dict) -> Optional[dict]:
    """Decide whether the first component is ad-valorem, specific, or free."""
    comps = measure.get("components") or []
    if not comps:
        return None
    c = comps[0]
    amt = c.get("rate_amount")
    mu = c.get("measurement_unit_code")
    monu = c.get("monetary_unit_code")
    if amt is None:
        return None
    if mu and monu:
        return {
            "kind": "specific",
            "rate_pct": None,
            "amount": float(amt),
            "per_unit": mu,
            "monetary_unit": monu,
            "text": f"{amt} {monu} / {mu}",
        }
    return {
        "kind": "ad_valorem",
        "rate_pct": float(amt),
        "amount": float(amt),
        "per_unit": None,
        "monetary_unit": None,
        "text": f"{amt:.2f} %",
    }


def best_applicable_rate(code: str, country_code: str, as_of: str | None = None) -> dict:
    """Pull applicable measures from the local DB and pick the best MFN + preference."""
    measures = applicable_measures(code, country_code, as_of=as_of)
    com = commodity(code)
    mfn: Optional[dict] = None
    pref: Optional[dict] = None
    vat: Optional[float] = None
    sup_code: Optional[str] = supplementary_unit_for(code, as_of=as_of)

    for m in measures:
        rate = _component_rate(m)
        mt = m["measure_type_id"]
        if mt == MFN_TYPE and rate is not None:
            if mfn is None or (rate["kind"] == "ad_valorem" and mfn["kind"] == "ad_valorem"
                               and (rate.get("rate_pct") or 0) < (mfn.get("rate_pct") or 0)):
                mfn = rate
        elif mt == PREFERENCE_TYPE and rate is not None:
            cand = {
                **rate,
                "measure_id": m["measure_sid"],
                "geographical_area_id": m["geographical_area_id"],
                "geographical_area_description": m["geographical_area_description"],
            }
            def _score(r):
                if r["kind"] == "ad_valorem":
                    return float(r.get("rate_pct") or 0)
                return float(r.get("amount") or 0)
            if pref is None or _score(rate) < _score(pref):
                pref = cand
        elif mt in VAT_TYPES and rate is not None and rate["kind"] == "ad_valorem":
            v = rate.get("rate_pct")
            if v is not None and (vat is None or v > vat):
                vat = v

    return {
        "code": _flat(code),
        "code_dotted": _dotted(code),
        "description": (com or {}).get("description", ""),
        "mfn": mfn,
        "preference": pref,
        "vat_rate": vat if vat is not None else 20.0,
        "supplementary_unit_code": sup_code,
        "all_measures": measures,
    }


# --- Export fixture fallback -------------------------------------------
#
# The full product app can be reviewed or deployed before the operator wires in
# a full `uk` tariff schema. In that mode, the classification workflow classification workflow runs against
# the bundled thin-slice facet data. Live Postgres remains preferred whenever it
# is reachable, unless CLASSIFICATION_FIXTURE_MODE=1 is set.

DATA_DIR = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def _fixture_commodities_raw() -> list[dict]:
    return json.loads((DATA_DIR / "commodities.json").read_text())["commodities"]


@lru_cache(maxsize=1)
def _fixture_commodities() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in _fixture_commodities_raw():
        dotted = item["code"]
        flat = _flat(dotted)
        out[dotted] = item
        out[flat] = item
    return out


@lru_cache(maxsize=1)
def _fixture_countries() -> list[dict]:
    return json.loads((DATA_DIR / "countries.json").read_text())["countries"]


@lru_cache(maxsize=1)
def _fixture_facets() -> dict:
    return json.loads((DATA_DIR / "facets.json").read_text())["facets"]


@lru_cache(maxsize=1)
def _fixture_edges() -> list[dict]:
    return json.loads((DATA_DIR / "kg_edges.json").read_text())["edges"]


@lru_cache(maxsize=1)
def _live_db_available() -> bool:
    if os.environ.get("CLASSIFICATION_FIXTURE_MODE") == "1":
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception:
        return False


def _fixture_item(code: str) -> Optional[dict]:
    return _fixture_commodities().get(code) or _fixture_commodities().get(_flat(code)) or _fixture_commodities().get(_dotted(code))


def _fixture_rate(rate: float | int | None, *, group: str = "MFN", measure_id: str = "fixture") -> dict:
    pct = float(rate or 0)
    return {
        "kind": "free" if pct == 0 else "ad_valorem",
        "rate_pct": pct,
        "amount": pct,
        "per_unit": None,
        "monetary_unit": None,
        "text": "Free" if pct == 0 else f"{pct:.2f} %",
        "measure_id": measure_id,
        "geographical_area_id": group,
        "geographical_area_description": group,
    }


def _fixture_retrieve_candidates(query: str, limit: int = 80) -> list[dict]:
    stopwords = {
        "of", "to", "the", "and", "or", "with", "from", "for", "in", "on", "by",
        "each", "goods", "import", "imports", "imported", "made",
    }
    tokens = {
        t
        for t in re.findall(r"[a-z0-9]+", (query or "").lower())
        if len(t) > 1 and t not in stopwords and not t.isdigit()
    }
    rows: list[dict] = []
    for item in _fixture_commodities_raw():
        hay = " ".join([
            item.get("code", ""),
            item.get("description", ""),
            item.get("self_text", ""),
            " ".join(item.get("common_terms") or []),
            " ".join(str(v) for v in (item.get("facets") or {}).values()),
        ]).lower()
        score = sum(3 if t in " ".join(item.get("common_terms") or []).lower() else 1 for t in tokens if t in hay)
        if score <= 0 and query.strip():
            continue
        flat = _flat(item["code"])
        rows.append({
            "commodity_code": flat,
            "description": item.get("description", ""),
            "score": float(score or 0.1),
            "sources": ["fixture_facets"],
            "source": "fixture_facets",
        })
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[:limit]


_live_health = health
_live_commodity = commodity
_live_countries = countries
_live_country_groups = country_groups
_live_facets_for = facets_for
_live_facets_for_codes = facets_for_codes
_live_kg_edges_for_candidates = kg_edges_for_candidates
_live_facet_definitions = facet_definitions
_live_supplementary_unit_for = supplementary_unit_for
_live_best_applicable_rate = best_applicable_rate
_live_import_requirements = import_requirements
_live_cert_documents_for_chain = _cert_documents_for_chain


_DEMO_EXAMPLE_CODES = [
    "1806907090",
    "1901200000",
    "2106909849",
    "2103909089",
    "4016999790",
    "7326909290",
]

_DEMO_PERSONAS = [
    {"id": "emu_generic", "label": "L1 emulator generic"},
    {"id": "emu_ordinary", "label": "L2 emulator ordinary"},
    {"id": "emu_specific", "label": "L3 emulator specific"},
    {"id": "naive_vague", "label": "L4 naive vague"},
    {"id": "naive_branded", "label": "L5 naive branded"},
    {"id": "naive_specific", "label": "L6 naive specific"},
    {"id": "original", "label": "L7 original ATAR"},
]


def classification_demo_personas() -> list[dict]:
    return list(_DEMO_PERSONAS)


def _normal_demo_persona(persona: str | None) -> str:
    ids = {p["id"] for p in _DEMO_PERSONAS}
    return persona if persona in ids else "emu_ordinary"


def classification_demo_examples(persona: str | None = None) -> list[dict]:
    """Demo prompts from the live KG/eval corpus.

    This is what the classification workflow classification workflow should prefer for the local demo. The bundled
    JSON examples remain a fallback for recipients who have not imported the
    `uk` + `kg` database yet.
    """
    if not _live_db_available():
        return []
    selected_persona = _normal_demo_persona(persona)
    examples: list[dict] = []
    try:
        with _conn() as c, c.cursor() as cur:
            for code in _DEMO_EXAMPLE_CODES:
                cur.execute(
                    f"""
                    SELECT expected_code, expected_description, persona, query, source_id
                    FROM {KG_SCHEMA}.eval_gold
                    WHERE expected_code = %s AND persona = %s
                    ORDER BY id
                    LIMIT 1
                    """,
                    (code, selected_persona),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute(
                        f"""
                        SELECT expected_code, expected_description, persona, query, source_id
                        FROM {KG_SCHEMA}.eval_gold
                        WHERE expected_code = %s
                    ORDER BY CASE persona
                      WHEN 'emu_specific' THEN 0
                      WHEN 'naive_specific' THEN 2
                      WHEN 'emu_ordinary' THEN 3
                      WHEN 'original' THEN 4
                      ELSE 9
                    END, id
                    LIMIT 1
                    """,
                        (code,),
                    )
                    row = cur.fetchone()
                if not row:
                    continue
                cur.execute(
                    f"SELECT count(*) AS n FROM {KG_SCHEMA}.commodity_facets WHERE commodity_code = %s",
                    (code,),
                )
                facet_count = int((cur.fetchone() or {}).get("n") or 0)
                commodity_row = _live_commodity(code) or {}
                examples.append(_classification_example_from_eval(row, commodity_row, facet_count))
    except Exception as exc:
        print(f"[classification examples] live KG lookup failed: {exc}")
        return []
    return examples


def _classification_example_from_eval(row: dict, commodity_row: dict, facet_count: int) -> dict:
    code = row["expected_code"]
    raw_query = (row["query"] or "").strip()
    query = _demo_prompt_query(code, raw_query, row.get("persona"), commodity_row.get("description") or row.get("expected_description") or "")
    description = commodity_row.get("description") or row.get("expected_description") or ""
    label = _demo_label(code, description)
    seed = {
        "description_of_goods": _demo_goods_description(code, query, description),
    }
    if code == "1806907090":
        seed.update({
            "quantity_units": 120,
            "quantity_unit_type": "KGM",
            "invoice_value": 1850,
            "invoice_currency": "GBP",
            "fx_rate_to_gbp": 1,
            "freight_gbp": 180,
            "insurance_gbp": 35,
            "other_costs_gbp": 0,
            "net_mass_kg": 120,
            "meursing_inputs": {
                "starch_glucose_pct": 18,
                "sucrose_invert_isoglucose_pct": 8,
                "milk_fat_pct": 1,
                "milk_protein_pct": 6,
                "additional_code": "7046",
            },
            "description_of_goods": "Chocolate and soy protein isolate powder in 1kg retail tubs",
        })
    return {
        "id": f"kg_{code}",
        "label": label,
        "query": query,
        "expected_code": code,
        "expected_code_dotted": _dotted(code),
        "description": description,
        "source": "live_kg",
        "source_detail": row.get("source_id") or f"kg.eval_gold:{row.get('persona')}",
        "persona": row.get("persona"),
        "facet_count": facet_count,
        "seed": seed,
    }


def _demo_label(code: str, description: str) -> str:
    labels = {
        "1806907090": "Complex protein powder",
        "1901200000": "Cheese dough balls",
        "2106909849": "Pre-workout powder",
        "2103909089": "Table sauce/chutney",
        "4016999790": "Rubber bellows",
        "7326909290": "Steel machine part",
    }
    return labels.get(code, description[:32] or code)


def _demo_goods_description(code: str, query: str, fallback: str) -> str:
    if code == "1806907090":
        return "Chocolate and soy protein isolate powder in 1kg retail tubs"
    text = _clean_demo_prompt_text(query)
    if len(text) > 180:
        text = _truncate_demo_prompt(text, 180)
    return text or fallback or code


def _demo_prompt_query(code: str, query: str, persona: str | None, fallback: str) -> str:
    """Trader-facing prompt text for live KG examples.

    eval_gold stores some ATAR-original rows in extraction language such as
    "product: the product is ...". Keep that raw row traceable, but never put
    it in the input box as the demo prompt.
    """
    text = _clean_demo_prompt_text(query)
    if persona == "original":
        # Original ATAR descriptions are intentionally detailed; cap them so an
        # input field remains usable while still showing richer product facts.
        if len(text) > 320:
            text = _truncate_demo_prompt(text, 320)
    return text or fallback or code


def _clean_demo_prompt_text(text: str) -> str:
    text = str(text or "").strip()
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:product|goods|description|query)\s*:\s*", "", text, flags=re.I)
    text = re.sub(
        r"^(?:the\s+)?product\s+(?:is|are)\s+(?:a|an|the)?\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    text = text.strip(" -")
    if text:
        text = text[0].upper() + text[1:]
        text = re.sub(
            r"(^|[.!?]\s+)([a-z])",
            lambda m: m.group(1) + m.group(2).upper(),
            text,
        )
    return text


def _truncate_demo_prompt(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 3].rstrip(" ,.;")
    boundary = cut.rfind(" ")
    if boundary >= max_len * 0.65:
        cut = cut[:boundary].rstrip(" ,.;")
    return cut + "..."


def health() -> dict:
    if _live_db_available():
        return _live_health()
    return {
        "ok": True,
        "schema": "fixture",
        "total": len(_fixture_commodities_raw()),
        "embedded": 0,
        "mode": "fixture",
        "note": "Using bundled product fixture data; live tariff_db is not reachable.",
    }


def retrieve_candidates(*args, **kwargs) -> list[dict]:
    query = args[0] if args else kwargs.get("query", "")
    limit = args[1] if len(args) > 1 else kwargs.get("limit", 80)
    if _live_db_available():
        return _live_retrieve_candidates(*args, **kwargs)
    return _fixture_retrieve_candidates(str(query), int(limit))


def commodity(code: str) -> Optional[dict]:
    if _live_db_available():
        return _live_commodity(code)
    item = _fixture_item(code)
    if not item:
        return None
    flat = _flat(item["code"])
    return {
        "sid": f"fixture:{flat}",
        "code": flat,
        "code_dotted": item["code"],
        "description": item.get("description", ""),
        "validity_start_date": None,
        "validity_end_date": None,
    }


def countries() -> list[dict]:
    if _live_db_available():
        return _live_countries()
    return [{"code": c["code"], "name": c["name"], "sid": f"fixture:{c['code']}"} for c in _fixture_countries()]


def country_groups(country_code: str, as_of: str | None = None) -> list[dict]:
    if _live_db_available():
        return _live_country_groups(country_code, as_of=as_of)
    row = next((c for c in _fixture_countries() if c["code"] == country_code), None)
    groups = (row or {}).get("groups") or ["MFN"]
    return [{"code": g, "name": g, "sid": f"fixture:{g}"} for g in groups]


def facets_for(code: str) -> list[dict]:
    if _live_db_available():
        return _live_facets_for(code)
    item = _fixture_item(code)
    if not item:
        return []
    out: list[dict] = []
    for k, v in (item.get("facets") or {}).items():
        use_scopes, evidence_roles = facet_labels("fixture", k, v)
        out.append({
            "commodity_code": _flat(item["code"]),
            "facet_key": k,
            "facet_value": v,
            "source": "fixture",
            "confidence": 1.0,
            "evidence": item.get("self_text", ""),
            "facet_label": (_fixture_facets().get(k) or {}).get("label", k),
            "authority_tier": 6,
            "use_scopes": use_scopes,
            "evidence_roles": evidence_roles,
            "provenance": {"source_type": "fixture"},
        })
    return out


def facets_for_codes(codes: list[str]) -> dict[str, list[dict]]:
    if _live_db_available():
        return _live_facets_for_codes(codes)
    return {_flat(c): facets_for(c) for c in codes}


def kg_edges_for_candidates(codes: list[str], include: Optional[dict] = None) -> list[dict]:
    if _live_db_available():
        return _live_kg_edges_for_candidates(codes, include=include)
    inc = {
        "chapter_notes": True, "section_notes": True, "legacy_blob_notes": True,
        "girs": True, "atar_rationales": True, "heading_rules": True, "other_global": True,
        "hsen": True,
    }
    if include:
        inc.update(include)
    flats = [_flat(c) for c in codes]
    items = [_fixture_item(c) for c in flats]
    explicit = {edge_id for item in items if item for edge_id in item.get("kg_edges", [])}
    chapters = {f[:2] for f in flats}
    headings = {f[:4] for f in flats}
    out = []
    for edge in _fixture_edges():
        scope = edge.get("scope", "")
        edge_id = edge.get("id") or ""
        edge_type = edge.get("type") or ""
        source = edge.get("source") or ""
        if "HSEN" in source and not inc.get("hsen", True):
            continue
        if edge_id.startswith("gir_") and not inc.get("girs", True):
            continue
        if edge_type == "rationale" and edge_id.startswith("atar_") and not inc.get("atar_rationales", True):
            continue
        if scope == "global" and not edge_id.startswith("gir_") and not inc.get("other_global", True):
            continue
        if (
            edge_id in explicit
            or scope == "global"
            or (not edge_id.startswith("atar_") and scope.removeprefix("chapter:") in chapters)
            or scope.removeprefix("heading:") in headings
        ):
            labelled = dict(edge)
            authority_tier = labelled.get("authority_tier")
            use_scopes, evidence_roles = edge_labels(
                edge_type,
                int(authority_tier or 8),
                source=source,
                edge_id=edge_id,
                scope=scope,
            )
            if not labelled.get("use_scopes"):
                labelled["use_scopes"] = use_scopes
            if not labelled.get("evidence_roles"):
                labelled["evidence_roles"] = evidence_roles
            if not labelled.get("provenance"):
                labelled["provenance"] = {"source_type": "fixture"}
            out.append(labelled)
    return out


def facet_definitions(keys: Optional[list[str]] = None) -> list[dict]:
    if _live_db_available():
        return _live_facet_definitions(keys)
    rows = []
    for key, meta in _fixture_facets().items():
        if keys and key not in keys:
            continue
        rows.append({
            "key": key,
            "label": meta.get("label", key),
            "short_label": meta.get("short_label", key),
            "value_set": meta.get("values", []),
            "applies_to_chapters": meta.get("applies_to_chapters", []),
            "rank": meta.get("rank", 999),
        })
    return sorted(rows, key=lambda r: r["rank"])


def supplementary_unit_for(code: str, as_of: str | None = None) -> Optional[str]:
    if _live_db_available():
        return _live_supplementary_unit_for(code, as_of=as_of)
    item = _fixture_item(code)
    return item.get("supplementary_unit") if item else None


def best_applicable_rate(code: str, country_code: str, as_of: str | None = None) -> dict:
    if _live_db_available():
        return _live_best_applicable_rate(code, country_code, as_of=as_of)
    item = _fixture_item(code)
    if not item:
        return {
            "code": _flat(code),
            "code_dotted": _dotted(code),
            "description": "",
            "mfn": None,
            "preference": None,
            "vat_rate": 20.0,
            "supplementary_unit_code": None,
            "all_measures": [],
        }
    groups = [g["code"] for g in country_groups(country_code)]
    preferences = item.get("preferences") or {}
    chosen_group = next((g for g in groups if g in preferences), None)
    pref = _fixture_rate(preferences[chosen_group], group=chosen_group, measure_id=f"fixture-pref-{chosen_group}") if chosen_group else None
    mfn = _fixture_rate(item.get("rate_mfn"), group="MFN", measure_id="fixture-mfn")
    all_measures = [
        {
            "measure_sid": "fixture-mfn",
            "measure_type_id": MFN_TYPE,
            "measure_type_description": "Third country rate (fixture)",
            "geographical_area_id": "1011",
            "geographical_area_description": "MFN",
            "components": [{"rate_amount": item.get("rate_mfn") or 0}],
        }
    ]
    if pref:
        all_measures.append({
            "measure_sid": pref["measure_id"],
            "measure_type_id": PREFERENCE_TYPE,
            "measure_type_description": "Preference rate (fixture)",
            "geographical_area_id": chosen_group,
            "geographical_area_description": chosen_group,
            "components": [{"rate_amount": pref.get("rate_pct") or 0}],
        })
    return {
        "code": _flat(item["code"]),
        "code_dotted": item["code"],
        "description": item.get("description", ""),
        "mfn": mfn,
        "preference": pref,
        "vat_rate": float(item.get("vat_rate") or 20.0),
        "supplementary_unit_code": item.get("supplementary_unit"),
        "all_measures": all_measures,
    }


def import_requirements(
    code: str,
    country_code: str,
    preference_claimed: Optional[str] = None,
    as_of: str | None = None,
) -> dict:
    if _live_db_available():
        return _live_import_requirements(code, country_code, preference_claimed, as_of=as_of)
    return {"measures": [], "rate_measures": [], "cert_documents": []}


def _cert_documents_for_chain(flat_code: str, as_of: str | None = None) -> list[dict]:
    if _live_db_available():
        return _live_cert_documents_for_chain(flat_code, as_of=as_of)
    return []
