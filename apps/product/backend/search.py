"""Local tariff retrieval for prompt authoring.

Pulls the top-N candidate commodity codes for a raw trader query using the
local tariff database and, when configured, a local OpenSearch keyword index.
The result format matches the existing `search_contexts.json` shape so it can
be fed straight into benchmark runs.

Production AI Search uses hybrid retrieval (OpenSearch BM25 + pgvector + RRF
fusion). This module mirrors that shape with a local OpenSearch BM25 leg and a
local pgvector cosine leg. If OpenSearch is not configured or is unavailable,
it falls back to Postgres full-text search for the keyword leg.
"""

from __future__ import annotations

import os
from typing import Any

import asyncpg
import httpx
from openai import AsyncOpenAI

from _retry import with_retry_and_limit

# Default connection to the local tariff_db. Overridable via env for CI.
_DSN = os.environ.get(
    "TARIFF_DB_DSN",
    "postgresql:///tariff_db",
)
_EMBEDDING_MODEL = "text-embedding-3-small"
_OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "").rstrip("/")
_OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "tariff_commodities")
_OPENSEARCH_TIMEOUT = float(os.environ.get("OPENSEARCH_TIMEOUT_SECONDS", "3"))

_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=_DSN,
            min_size=1,
            max_size=4,
            command_timeout=30,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def embed_query(client: AsyncOpenAI, text: str) -> list[float]:
    """Generate a 1536-d embedding for a raw query via OpenAI. Routed to
    its own "openai_embedding" pool so authoring-time embeddings don't steal
    slots from model/judge calls during a running benchmark."""
    resp = await with_retry_and_limit(
        "openai_embedding",
        lambda: client.embeddings.create(model=_EMBEDDING_MODEL, input=text),
    )
    return resp.data[0].embedding


async def _vector_leg(
    conn: asyncpg.Connection, embedding_literal: str, over_fetch: int
) -> list[dict[str, Any]]:
    """pgvector HNSW cosine search - semantic matching."""
    rows = await conn.fetch(
        """
        SELECT
            st.goods_nomenclature_item_id AS commodity_code,
            st.self_text AS description,
            1 - (st.search_embedding <=> $1::vector) AS score
        FROM uk.goods_nomenclature_self_texts st
        JOIN uk.goods_nomenclatures gn
          ON gn.goods_nomenclature_sid = st.goods_nomenclature_sid
        WHERE st.search_embedding IS NOT NULL
          AND coalesce(st.expired, false) = false
          AND coalesce(st.stale, false) = false
          AND gn.validity_start_date::date <= current_date
          AND (
            gn.validity_end_date IS NULL
            OR gn.validity_end_date::date >= current_date
          )
        ORDER BY st.search_embedding <=> $1::vector
        LIMIT $2
        """,
        embedding_literal,
        over_fetch,
    )
    return [
        {
            "commodity_code": r["commodity_code"],
            "description": r["description"],
            "score": float(r["score"]),
        }
        for r in rows
    ]


async def _keyword_leg(
    conn: asyncpg.Connection, raw_query: str, over_fetch: int
) -> list[dict[str, Any]]:
    """Postgres full-text fallback for the keyword leg."""
    rows = await conn.fetch(
        """
        SELECT
            st.goods_nomenclature_item_id AS commodity_code,
            st.self_text AS description,
            ts_rank_cd(
                to_tsvector('english', st.self_text),
                plainto_tsquery('english', $1)
            ) AS score
        FROM uk.goods_nomenclature_self_texts st
        JOIN uk.goods_nomenclatures gn
          ON gn.goods_nomenclature_sid = st.goods_nomenclature_sid
        WHERE to_tsvector('english', st.self_text) @@ plainto_tsquery('english', $1)
          AND coalesce(st.expired, false) = false
          AND coalesce(st.stale, false) = false
          AND gn.validity_start_date::date <= current_date
          AND (
            gn.validity_end_date IS NULL
            OR gn.validity_end_date::date >= current_date
          )
        ORDER BY score DESC
        LIMIT $2
        """,
        raw_query,
        over_fetch,
    )
    return [
        {
            "commodity_code": r["commodity_code"],
            "description": r["description"],
            "score": float(r["score"]),
        }
        for r in rows
    ]


async def _opensearch_keyword_leg(
    raw_query: str, over_fetch: int
) -> list[dict[str, Any]] | None:
    """OpenSearch BM25 keyword leg.

    Returns None when OpenSearch is not configured or unreachable so callers can
    fall back to the Postgres FTS keyword leg without failing the whole preview.
    """
    if not _OPENSEARCH_URL:
        return None
    payload = {
        "size": over_fetch,
        "_source": ["commodity_code", "self_text", "search_text"],
        "query": {
            "multi_match": {
                "query": raw_query,
                "fields": ["commodity_code^4", "self_text^3", "search_text"],
                "operator": "or",
            }
        },
    }
    try:
        async with httpx.AsyncClient(timeout=_OPENSEARCH_TIMEOUT) as http:
            res = await http.post(
                f"{_OPENSEARCH_URL}/{_OPENSEARCH_INDEX}/_search",
                json=payload,
            )
            if res.status_code == 404:
                return None
            res.raise_for_status()
    except Exception:
        return None

    hits = res.json().get("hits", {}).get("hits", [])
    out: list[dict[str, Any]] = []
    for hit in hits:
        src = hit.get("_source") or {}
        code = src.get("commodity_code")
        if not code:
            continue
        out.append(
            {
                "commodity_code": str(code),
                "description": str(src.get("self_text") or src.get("search_text") or ""),
                "score": float(hit.get("_score") or 0.0),
            }
        )
    return out


def _rrf_fuse(
    vector_results: list[dict[str, Any]],
    keyword_results: list[dict[str, Any]],
    limit: int,
    k: int = 60,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion - the same aggregation production uses to
    combine the OpenSearch and pgvector legs. For each doc, score is the
    sum of 1/(rank + k) across the legs it appears in. k=60 matches
    InteractiveSearchService defaults.
    """
    fused: dict[str, dict[str, Any]] = {}
    for leg in (vector_results, keyword_results):
        for rank, r in enumerate(leg, start=1):
            code = r["commodity_code"]
            if code not in fused:
                fused[code] = {
                    "commodity_code": code,
                    "description": r["description"],
                    "score": 0.0,
                }
            fused[code]["score"] += 1.0 / (rank + k)
    ranked = sorted(fused.values(), key=lambda x: x["score"], reverse=True)
    return ranked[:limit]


async def retrieve_candidates(
    client: AsyncOpenAI,
    raw_query: str,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Hybrid retrieval: pgvector cosine + Postgres full-text BM25, fused via
    RRF (k=60). Matches production InteractiveSearchService behaviour except
    for the lack of POS-tag boosts and structured-field boosts from OpenSearch.

    Over-fetches 2x from each leg so the fusion has material to work with
    before truncating to `limit`.
    """
    embedding = await embed_query(client, raw_query)
    embedding_literal = "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"
    over_fetch = min(limit * 2, 400)

    pool = await _get_pool()
    async with pool.acquire() as conn:
        vec = await _vector_leg(conn, embedding_literal, over_fetch)
        kw = await _opensearch_keyword_leg(raw_query, over_fetch)
        if kw is None:
            kw = await _keyword_leg(conn, raw_query, over_fetch)

    return _rrf_fuse(vec, kw, limit=limit)


async def probe_db() -> dict[str, Any]:
    """Quick health-check for the UI to verify the local tariff DB is reachable."""
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT count(*) AS total, count(st.search_embedding) AS embedded
                FROM uk.goods_nomenclature_self_texts st
                JOIN uk.goods_nomenclatures gn
                  ON gn.goods_nomenclature_sid = st.goods_nomenclature_sid
                WHERE st.self_text IS NOT NULL
                  AND length(trim(st.self_text)) > 0
                  AND coalesce(st.expired, false) = false
                  AND coalesce(st.stale, false) = false
                  AND gn.validity_start_date::date <= current_date
                  AND (
                    gn.validity_end_date IS NULL
                    OR gn.validity_end_date::date >= current_date
                  )
                """
            )
            probe = {
                "ok": True,
                "total": int(row["total"]),
                "embedded": int(row["embedded"]),
            }
            if _OPENSEARCH_URL:
                probe["opensearch"] = await probe_opensearch()
            return probe
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


async def probe_opensearch() -> dict[str, Any]:
    if not _OPENSEARCH_URL:
        return {"configured": False, "ok": False}
    try:
        async with httpx.AsyncClient(timeout=_OPENSEARCH_TIMEOUT) as http:
            health = await http.get(f"{_OPENSEARCH_URL}/_cluster/health")
            count = await http.get(f"{_OPENSEARCH_URL}/{_OPENSEARCH_INDEX}/_count")
            return {
                "configured": True,
                "ok": health.is_success and count.is_success,
                "status": health.json().get("status") if health.is_success else None,
                "index": _OPENSEARCH_INDEX,
                "count": count.json().get("count") if count.is_success else 0,
            }
    except Exception as exc:
        return {
            "configured": True,
            "ok": False,
            "index": _OPENSEARCH_INDEX,
            "error": str(exc)[:200],
        }
