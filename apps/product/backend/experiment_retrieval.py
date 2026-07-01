from __future__ import annotations

import csv
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
SCHEMA = os.environ.get("TARIFF_DB_SCHEMA", "uk")
KG_SCHEMA = os.environ.get("TARIFF_DB_KG_SCHEMA", "kg")
MATRIX_CSV = Path(__file__).parent.parent / "data" / "matrix" / "retrieval_matrix.csv"
KG_FACT_ALIAS_SOURCES = {"goods_nomenclature_labels", "search_reference"}

TOP_RUN_LABEL = "no_curated_only"
DEFAULT_LIMIT = 500
DISPLAY_LIMIT = 25


def _conn():
    return psycopg.connect(DSN, row_factory=dict_row)


@lru_cache(maxsize=32)
def _kg_has_column(table: str, column: str) -> bool:
    """Runtime-tolerant probe for optional KG evidence-label migrations."""
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


def _kg_fact_source_filter(alias: str) -> tuple[str, list[str]]:
    excluded = sorted(KG_FACT_ALIAS_SOURCES)
    return f"AND {alias}.source <> ALL(%s::text[])", excluded


def _flat_code(code: str) -> str:
    digits = re.sub(r"\D", "", code or "")
    return digits.ljust(10, "0")[:10] if digits else ""


def _pct(value: str) -> float:
    try:
        return round(float(value) * 100, 1)
    except Exception:
        return 0.0


def _load_matrix_rows() -> list[dict[str, Any]]:
    with MATRIX_CSV.open(newline="") as f:
        rows = list(csv.DictReader(f))
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            cfg = json.loads(row.get("config_json") or "{}")
        except json.JSONDecodeError:
            cfg = {}
        rank_raw = row.get("rank_by_code_macro_at_100") or ""
        rank = int(rank_raw) if rank_raw.isdigit() else None
        title, description, caveats = describe_experiment(row.get("run_label", ""), cfg)
        out.append(
            {
                "rank": rank,
                "run_label": row.get("run_label"),
                "run_id": row.get("run_id"),
                "title": title,
                "description": description,
                "caveats": caveats,
                "headline_recall_pct": _pct(row.get("code_macro_recall_at_100", "0")),
                "ott_baseline": str(row.get("ott_baseline", "")).lower() == "true",
                "runnable": is_runnable_config(cfg),
                "config": cfg,
            }
        )
    return out


def experiment_catalog() -> list[dict[str, Any]]:
    return _load_matrix_rows()


def top_experiment_info() -> dict[str, Any]:
    rows = _load_matrix_rows()
    for row in rows:
        if row["run_label"] == TOP_RUN_LABEL:
            return row
    return rows[0]


def select_experiment(run_label: str | None = None) -> dict[str, Any]:
    catalog = _load_matrix_rows()
    selected_label = run_label or TOP_RUN_LABEL
    for row in catalog:
        if row["run_label"] == selected_label:
            return row
    raise ValueError(f"Unknown experiment: {run_label}")


def experiment_requires_provider(run_label: str | None = None) -> bool:
    cfg = select_experiment(run_label)["config"]
    return bool(cfg.get("use_vector") or cfg.get("use_facts_vec") or cfg.get("use_kg_vec"))


def is_runnable_config(cfg: dict[str, Any]) -> bool:
    # Query rewrite / triage depends on the eval-time rewrite harness, which is
    # intentionally not included in this shareable app.
    return not bool(cfg.get("triage"))


def _enabled_parts(cfg: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    if cfg.get("use_composite"):
        parts.append("AI-enriched code text")
    if cfg.get("use_vector"):
        parts.append("semantic vector")
    if cfg.get("use_facts") or cfg.get("use_facts_vec"):
        parts.append("commodity facts")
    if cfg.get("use_kg_context") or cfg.get("use_kg_vec"):
        parts.append("KG")
    if cfg.get("use_curated"):
        parts.append("Search References")
    if cfg.get("triage"):
        parts.append("rewrite")
    return parts or ["keyword FTS"]


def describe_experiment(run_label: str, cfg: dict[str, Any]) -> tuple[str, str, list[str]]:
    known_titles = {
        "baseline_fts_only": "Classic keyword baseline",
        "staging_ai": "Production-style AI staging baseline",
        "rw_g41_staging": "Staging rewrite upper-bound baseline",
        "no_curated_only": "Top overall: semantic + KG + commodity facts, no Search References",
        "all_legs_on": "Semantic + KG + commodity facts",
        "ai_semantic_composite_triage": "AI-enriched semantic search with query rewrite",
        "rw_g5_staging": "Staging rewrite with GPT-5-class rewrite model",
        "rw_g5_mine": "Eval rewrite with GPT-5-class rewrite model",
    }

    title = known_titles.get(run_label)
    if not title:
        if run_label.startswith("facts_cap"):
            cap = cfg.get("facts_cap", "?")
            title = f"AI-enriched rewrite + facets capped at {cap}"
        elif run_label.startswith("grid_"):
            title = "Grid: " + " + ".join(_enabled_parts(cfg))
        elif run_label.startswith("exp2_off_"):
            removed = run_label.removeprefix("exp2_off_").replace("_", " ")
            title = f"Ablation: {removed} removed"
        elif run_label.startswith("exp2_rrf_"):
            title = f"RRF fusion sweep: {run_label.removeprefix('exp2_rrf_')}"
        elif run_label.startswith("exp3_"):
            title = f"Secondary-leg cap sweep: {run_label.removeprefix('exp3_').replace('_', ' ')}"
        else:
            title = run_label.replace("_", " ")

    signals: list[str] = []
    if cfg.get("use_composite"):
        signals.append("AI-enriched code text")
    else:
        signals.append("base commodity self-text")
    if cfg.get("use_vector"):
        signals.append("semantic vector search")
    else:
        signals.append("no description-vector leg")
    if cfg.get("use_curated", False):
        signals.append("Search References matches")
    elif "use_curated" in cfg:
        signals.append("Search References disabled")
    else:
        signals.append("Search References not flagged in the matrix config")
    if cfg.get("use_facts"):
        signals.append("structured commodity fact keyword matches")
    if cfg.get("use_facts_vec"):
        signals.append("structured commodity fact semantic matches")
    if cfg.get("use_kg_context"):
        signals.append("KG rule/note keyword matches")
    if cfg.get("use_kg_vec"):
        signals.append("KG rule/note semantic matches")
    if cfg.get("triage"):
        signals.append("query rewrite / triage before retrieval")

    description = "Uses " + "; ".join(signals) + "."
    caveats: list[str] = []
    if cfg.get("triage"):
        caveats.append("This row is described in the catalog but is not runnable in the local trial form because the rewrite harness is not bundled.")
    if cfg.get("retrieval_limit"):
        caveats.append(f"The matrix headline is recall@100; this run was evaluated with retrieval_limit={cfg['retrieval_limit']}.")
    return title, description, caveats


def _embed_query(text: str, api_key: str | None) -> list[float]:
    if not api_key:
        raise RuntimeError("OpenAI API key required for the top experiment because it uses semantic vector legs.")
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.embeddings.create(model="text-embedding-3-small", input=text)
    return response.data[0].embedding


def _fts_leg(query: str, limit: int) -> list[dict[str, Any]]:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT st.goods_nomenclature_item_id AS commodity_code,
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


def _description_substring_leg(query: str, limit: int) -> list[dict[str, Any]]:
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
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "substring",
            }
            for r in cur.fetchall()
        ]


def _curated_leg(query: str, limit: int) -> list[dict[str, Any]]:
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
            for r in cur.fetchall()
            if r["commodity_code"]
        ]


def _facts_leg(query: str, limit: int) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    source_filter, source_excl = _kg_fact_source_filter("cf")
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT cf.commodity_code,
                   max(
                     ts_rank_cd(
                       to_tsvector('english', cf.facet_key || ' ' || cf.facet_value || ' ' || COALESCE(cf.evidence, '')),
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
            WHERE to_tsvector('english', cf.facet_key || ' ' || cf.facet_value || ' ' || COALESCE(cf.evidence, '')) @@ q.tsq
              {source_filter}
              {_kg_use_scope_filter("cf", "commodity_facets", "retrieval")}
            GROUP BY cf.commodity_code
            ORDER BY score DESC
            LIMIT %s
            """,
            (query, source_excl, limit),
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


def _kg_context_leg(query: str, limit: int) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT kec.commodity_code,
                   max(
                     ts_rank_cd(to_tsvector('english', e.title || ' ' || e.body), q.tsq)
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
            WHERE to_tsvector('english', e.title || ' ' || e.body) @@ q.tsq
              {_kg_use_scope_filter("e", "kg_edges", "retrieval")}
            GROUP BY kec.commodity_code
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
                "source": "kg_context",
            }
            for r in cur.fetchall()
        ]


def _vector_leg(query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            SELECT st.goods_nomenclature_item_id AS commodity_code,
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


def _composite_fts_leg(query: str, limit: int) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH q AS (SELECT plainto_tsquery('english', %s) AS tsq)
            SELECT goods_nomenclature_item_id AS commodity_code,
                   composite_text AS description,
                   ts_rank_cd(to_tsvector('english', composite_text), q.tsq) AS score
            FROM {KG_SCHEMA}.composite_search_text, q
            WHERE to_tsvector('english', composite_text) @@ q.tsq
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
                "source": "fts_composite",
            }
            for r in cur.fetchall()
        ]


def _composite_vector_leg(query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
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
        return [
            {
                "commodity_code": r["commodity_code"],
                "description": (r["description"] or "")[:280],
                "score": float(r["score"] or 0),
                "source": "vector_composite",
            }
            for r in cur.fetchall()
        ]


def _facts_vec_leg(query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    fact_pool = limit * 4
    source_filter, source_excl = _kg_fact_source_filter("cf")
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH top_facts AS (
                SELECT cf.commodity_code,
                       cf.authority_tier,
                       1 - (cf.embedding <=> %s::vector) AS cosine
                FROM {KG_SCHEMA}.commodity_facets cf
                WHERE cf.embedding IS NOT NULL
                  {source_filter}
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
            (literal, source_excl, literal, fact_pool, limit),
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


def _kg_vec_leg(query_embedding: list[float], limit: int) -> list[dict[str, Any]]:
    literal = "[" + ",".join(f"{x:.6f}" for x in query_embedding) + "]"
    edge_pool = limit * 4
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            f"""
            WITH top_edges AS (
                SELECT e.id,
                       e.authority_tier,
                       1 - (e.embedding <=> %s::vector) AS cosine
                FROM {KG_SCHEMA}.kg_edges e
                WHERE e.embedding IS NOT NULL
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
            (literal, literal, edge_pool, limit),
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


def _rrf_fuse(legs: list[tuple[str, list[dict[str, Any]], float]], limit: int, k: int = 60) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    for _name, leg, cap in legs:
        for rank, row in enumerate(leg, start=1):
            code = row.get("commodity_code")
            if not code:
                continue
            if code not in fused:
                fused[code] = {
                    "commodity_code": code,
                    "description": row.get("description") or "",
                    "score": 0.0,
                    "sources": [],
                }
            fused[code]["score"] += cap * 1.0 / (rank + k)
            source = row.get("source")
            if source and source not in fused[code]["sources"]:
                fused[code]["sources"].append(source)
    return sorted(fused.values(), key=lambda x: -x["score"])[:limit]


def retrieve_for_config(query: str, cfg: dict[str, Any], api_key: str | None, limit: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if cfg.get("triage"):
        raise RuntimeError("This experiment uses query rewrite / triage and is not runnable in the local trial form.")
    if not query.strip():
        return [], {}

    legs: list[tuple[str, list[dict[str, Any]], float]] = []
    if cfg.get("use_curated", False):
        leg = _curated_leg(query, limit)
        legs.append(("reference", leg, 1.0))

    fts_fn = _composite_fts_leg if cfg.get("use_composite") else _fts_leg
    leg = fts_fn(query, limit)
    legs.append(("fts_composite" if cfg.get("use_composite") else "fts", leg, 1.0))

    leg = _description_substring_leg(query, limit)
    legs.append(("substring", leg, 1.0))

    needs_embedding = bool(cfg.get("use_vector") or cfg.get("use_facts_vec") or cfg.get("use_kg_vec"))
    embedding = _embed_query(query, api_key) if needs_embedding else None

    if cfg.get("use_vector") and embedding is not None:
        vector_fn = _composite_vector_leg if cfg.get("use_composite") else _vector_leg
        leg = vector_fn(embedding, limit)
        legs.append(("vector_composite" if cfg.get("use_composite") else "vector", leg, 1.0))
    if cfg.get("use_facts"):
        leg = _facts_leg(query, limit)
        legs.append(("facts", leg, float(cfg.get("facts_cap", 0.5))))
    if cfg.get("use_kg_context"):
        leg = _kg_context_leg(query, limit)
        legs.append(("kg_context", leg, float(cfg.get("kg_cap", 0.5))))
    if cfg.get("use_facts_vec") and embedding is not None:
        leg = _facts_vec_leg(embedding, limit)
        legs.append(("facts_vec", leg, float(cfg.get("facts_vec_cap", 0.6))))
    if cfg.get("use_kg_vec") and embedding is not None:
        leg = _kg_vec_leg(embedding, limit)
        legs.append(("kg_vec", leg, float(cfg.get("kg_vec_cap", 0.6))))

    leg_counts = {name: len(rows) for name, rows, _cap in legs}
    fused = _rrf_fuse(legs, limit=limit, k=int(cfg.get("rrf_k", 60)))
    return fused, leg_counts


def run_trial(
    query: str,
    expected_code: str,
    api_key: str | None,
    run_label: str | None = None,
    retrieval_limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    query = query.strip()
    expected_flat = _flat_code(expected_code)
    if not query:
        raise ValueError("query is required")

    selected = select_experiment(run_label)

    limit = max(10, min(int(retrieval_limit or DEFAULT_LIMIT), DEFAULT_LIMIT))
    candidates, leg_counts = retrieve_for_config(query, selected["config"], api_key, limit)
    rank = None
    if expected_flat:
        for idx, row in enumerate(candidates, start=1):
            if _flat_code(row["commodity_code"]) == expected_flat:
                rank = idx
                break

    ranked_candidates = []
    for idx, row in enumerate(candidates, start=1):
        item = dict(row)
        item["rank"] = idx
        ranked_candidates.append(item)

    return {
        "query": query,
        "expected_code": expected_code,
        "expected_code_normalized": expected_flat,
        "evaluated": bool(expected_flat),
        "experiment": selected,
        "retrieval_limit": limit,
        "rank": rank,
        "hit_at_10": bool(rank and rank <= 10),
        "hit_at_100": bool(rank and rank <= 100),
        "hit_within_limit": rank is not None,
        "leg_counts": leg_counts,
        "top_candidates": ranked_candidates[:DISPLAY_LIMIT],
        "candidates": ranked_candidates,
    }
