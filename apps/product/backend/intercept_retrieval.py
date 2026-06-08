"""Hybrid retrieval that mirrors production HybridRetrievalService.

Vector leg: bit-exact replication of VectorRetrievalService (pgvector cosine
against search_embedding, ef_search=100, score threshold 0.35, producline_suffix='80',
hidden goods excluded).

Keyword leg: Postgres FTS websearch_to_tsquery on search_text. This is an
APPROXIMATION of production's OpenSearch BM25 leg — it lacks query expansion,
POS-aware boosting, and search labels. Documented gap.

RRF fusion: 1/(rank + k), k=60, deduped by goods_nomenclature_sid. Identical
to HybridRetrievalService#rrf_merge.

Post-filter to leaves (declarable = producline_suffix='80' AND no children in tree).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import asyncpg
from openai import AsyncOpenAI

# Production-aligned defaults (verified in trade-tariff-backend/app/models/admin_configuration.rb)
RRF_K = 60
VECTOR_EF_SEARCH = 100
VECTOR_SCORE_THRESHOLD = 0.35
NON_GROUPING_PRODUCTLINE_SUFFIX = "80"
EMBEDDING_MODEL = "text-embedding-3-small"

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")


@dataclass
class Candidate:
    sid: int
    code: str
    self_text: str | None
    search_text: str | None
    score: float  # RRF score
    cosine_score: float | None = None  # raw cosine sim from vector leg
    bm25_rank: int | None = None
    vector_rank: int | None = None
    declarable: bool | None = None
    generation_type: str | None = None  # 'ai' = AI-166 contextualised Other; 'ai_non_other' = regular

    def to_dict(self) -> dict[str, Any]:
        return {
            "goods_nomenclature_sid": self.sid,
            "goods_nomenclature_item_id": self.code,
            "self_text": self.self_text,
            "search_text": self.search_text,
            "score": self.score,
            "cosine_score": self.cosine_score,
            "bm25_rank": self.bm25_rank,
            "vector_rank": self.vector_rank,
            "declarable": self.declarable,
            "generation_type": self.generation_type,
        }


class Retriever:
    """Hybrid retrieval client. Manages pgvector pool, OpenAI client, and
    a pre-computed declarable-leaf set."""

    def __init__(self, dsn: str = DSN, openai_client: AsyncOpenAI | None = None):
        self.dsn = dsn
        self.openai = openai_client or AsyncOpenAI()
        self._pool: asyncpg.Pool | None = None
        self._hidden_codes: set[str] = set()
        self._declarable_sids: set[int] | None = None
        self._chapter_to_section: dict[str, str] = {}
        self._indent_depth_by_sid: dict[int, int] = {}
        self._section_titles: dict[str, str] = {}   # numeral -> title
        self._descriptions_by_code: dict[str, str] = {}   # 10-digit padded code -> description
        self._contextualised_by_code: dict[str, str] = {} # 10-digit code -> AI-166 contextualised self_text (any level)

    async def setup(self) -> None:
        # Pool sized for the parallel commodity sweep: each worker can be
        # holding two connections at once (vector_leg + keyword_leg). At
        # concurrency=40 the peak demand is ~80, so we headroom to 64.
        self._pool = await asyncpg.create_pool(dsn=self.dsn, min_size=4, max_size=64)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT goods_nomenclature_item_id FROM uk.hidden_goods_nomenclatures"
            )
            self._hidden_codes = {r["goods_nomenclature_item_id"] for r in rows}

            # Chapter (2-digit) -> Section roman numeral. Used by KPIs to add
            # a section-level entropy/inflexion signal above the chapter.
            chap_sec = await conn.fetch(
                """
                SELECT
                    LEFT(gn.goods_nomenclature_item_id, 2) AS chapter_digits,
                    s.numeral AS section_numeral
                FROM uk.chapters_sections cs
                JOIN uk.sections s ON s.id = cs.section_id
                JOIN uk.goods_nomenclatures gn ON gn.goods_nomenclature_sid = cs.goods_nomenclature_sid
                """
            )
            self._chapter_to_section = {r["chapter_digits"]: r["section_numeral"] for r in chap_sec}

            # Section roman -> title. Used to surface the section name in the
            # tree breadcrumbs and hover differentiator question.
            sect_rows = await conn.fetch(
                "SELECT numeral, title FROM uk.sections WHERE numeral IS NOT NULL"
            )
            self._section_titles = {r["numeral"]: r["title"] for r in sect_rows}

            # Description for every commodity code at every level. Lets the
            # frontend render a meaningful label per tree box (Chapter 90 =
            # "OPTICAL, PHOTOGRAPHIC...", Heading 9026 = "Instruments for
            # measuring or checking..."). Lookup key = 10-digit padded code.
            desc_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (goods_nomenclature_item_id)
                    goods_nomenclature_item_id,
                    description
                FROM uk.goods_nomenclature_descriptions
                WHERE productline_suffix = $1
                """,
                NON_GROUPING_PRODUCTLINE_SUFFIX,
            )
            self._descriptions_by_code = {
                r["goods_nomenclature_item_id"]: r["description"] for r in desc_rows
            }

            # AI-166 contextualised self_texts at EVERY level (not just declarable
            # leaves). The raw description for an "Other" subheading like
            # 8536699000 is literally "Other"; the AI-166 self_text gives it a
            # real label ("Electrical plugs and sockets for a voltage <= 1000 V
            # (excl. ...)"). We prefer the self_text whenever generation_type
            # is 'ai' so intermediate tree boxes don't surface as "Other".
            ctx_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (gn.goods_nomenclature_item_id)
                    gn.goods_nomenclature_item_id AS code,
                    st.self_text
                FROM uk.goods_nomenclature_self_texts st
                JOIN uk.goods_nomenclatures gn USING (goods_nomenclature_sid)
                WHERE st.generation_type = 'ai'
                  AND st.self_text IS NOT NULL
                  AND gn.producline_suffix = $1
                """,
                NON_GROUPING_PRODUCTLINE_SUFFIX,
            )
            self._contextualised_by_code: dict[str, str] = {
                r["code"]: r["self_text"] for r in ctx_rows
            }

            # Tree depth per sid (for mean_indent_depth KPI).
            depth_rows = await conn.fetch(
                """
                SELECT goods_nomenclature_sid, number_indents
                FROM uk.goods_nomenclature_tree_nodes
                WHERE productline_suffix = $1
                """,
                NON_GROUPING_PRODUCTLINE_SUFFIX,
            )
            self._indent_depth_by_sid = {r["goods_nomenclature_sid"]: r["number_indents"] for r in depth_rows}

            # Precompute leaf set via goods_nomenclature_tree_nodes (preorder
            # traversal). A node is a leaf iff the next node by position has
            # depth <= this node's depth. ~35ms on 60k rows vs the @> nested
            # loop which would take minutes.
            leaf_rows = await conn.fetch(
                """
                WITH ordered AS (
                    SELECT
                        goods_nomenclature_sid,
                        depth,
                        position,
                        LEAD(depth) OVER (ORDER BY position) AS next_depth
                    FROM uk.goods_nomenclature_tree_nodes
                    WHERE productline_suffix = $1
                )
                SELECT goods_nomenclature_sid
                FROM ordered
                WHERE next_depth IS NULL OR next_depth <= depth
                """,
                NON_GROUPING_PRODUCTLINE_SUFFIX,
            )
            self._declarable_sids = {r["goods_nomenclature_sid"] for r in leaf_rows}

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()

    async def embed(self, query: str) -> list[float]:
        resp = await self.openai.embeddings.create(model=EMBEDDING_MODEL, input=query)
        return resp.data[0].embedding

    async def embed_batch(self, queries: list[str], batch_size: int = 2048) -> list[list[float]]:
        """Embed many queries in batched API calls. OpenAI's embeddings endpoint
        accepts up to 2048 inputs per request. Returns embeddings in the same
        order as the input list."""
        if not queries:
            return []
        embeddings: list[list[float]] = []
        for i in range(0, len(queries), batch_size):
            batch = queries[i:i + batch_size]
            resp = await self.openai.embeddings.create(model=EMBEDDING_MODEL, input=batch)
            embeddings.extend(d.embedding for d in resp.data)
        return embeddings

    async def retrieve_with_embedding(
        self,
        query: str,
        embedding: list[float],
        limit: int = 30,
        vector_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Same as retrieve() but skips the embed step — caller provides the
        embedding. Lets the batched-embed pipeline reuse pre-computed vectors."""
        global VECTOR_SCORE_THRESHOLD
        prev_threshold = VECTOR_SCORE_THRESHOLD
        try:
            if vector_threshold is not None:
                VECTOR_SCORE_THRESHOLD = float(vector_threshold)
            vec_task = asyncio.create_task(self.vector_leg(embedding, over_fetch=limit))
            kw_task = asyncio.create_task(self.keyword_leg(query, over_fetch=limit))
            vec, kw = await asyncio.gather(vec_task, kw_task)
            below = list(getattr(self, "_last_below_threshold", []) or [])
        finally:
            VECTOR_SCORE_THRESHOLD = prev_threshold

        fused = self.rrf_merge(vec, kw)
        declarable_only = [c for c in fused if c.declarable]
        return {
            "query": query,
            "vector_count": len(vec),
            "keyword_count": len(kw),
            "fused_count": len(fused),
            "declarable_count": len(declarable_only),
            "candidates": [c.to_dict() for c in declarable_only[:limit]],
            "vector_threshold_used": vector_threshold if vector_threshold is not None else prev_threshold,
            "below_threshold_candidates": below,
        }

    async def vector_leg(self, embedding: list[float], over_fetch: int) -> list[Candidate]:
        """Mirrors VectorRetrievalService: ef_search=100, producline=80, hidden
        codes excluded, ordered by cosine distance, capped at over_fetch."""
        literal = "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"
        assert self._pool
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f"SET LOCAL hnsw.ef_search = {VECTOR_EF_SEARCH}")
                rows = await conn.fetch(
                    f"""
                    SELECT
                        gn.goods_nomenclature_sid AS sid,
                        gn.goods_nomenclature_item_id AS code,
                        st.self_text,
                        st.search_text,
                        st.generation_type,
                        1 - (st.search_embedding <=> $1::vector) AS cosine_score
                    FROM uk.goods_nomenclature_self_texts st
                    JOIN uk.goods_nomenclatures gn USING (goods_nomenclature_sid)
                    WHERE st.search_embedding IS NOT NULL
                      AND gn.producline_suffix = $2
                      AND gn.goods_nomenclature_item_id NOT IN (
                          SELECT goods_nomenclature_item_id FROM uk.hidden_goods_nomenclatures
                      )
                    ORDER BY st.search_embedding <=> $1::vector
                    LIMIT $3
                    """,
                    literal,
                    NON_GROUPING_PRODUCTLINE_SUFFIX,
                    over_fetch,
                )
        cands = []
        below_threshold: list[dict[str, Any]] = []
        for rank, r in enumerate(rows):
            score = float(r["cosine_score"])
            if score < VECTOR_SCORE_THRESHOLD:
                if len(below_threshold) < 10:
                    below_threshold.append({
                        "goods_nomenclature_item_id": r["code"],
                        "search_text": r["search_text"],
                        "self_text": r["self_text"],
                        "cosine_score": score,
                        "vector_rank": rank + 1,
                    })
                continue
            cands.append(Candidate(
                sid=r["sid"], code=r["code"],
                self_text=r["self_text"], search_text=r["search_text"],
                generation_type=r["generation_type"],
                score=0.0, cosine_score=score, vector_rank=rank + 1,
            ))
        # Stash diagnostic for the retrieve() method to surface
        self._last_below_threshold = below_threshold
        return cands

    async def keyword_leg(self, query: str, over_fetch: int) -> list[Candidate]:
        """Postgres FTS approximation of OpenSearch BM25 leg.

        GAP vs production: no query expansion, no POS-aware noun/qualifier
        boosting, no search labels. Same filter set as the vector leg (so the
        candidate population is comparable, only the ranking differs)."""
        assert self._pool
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT
                    gn.goods_nomenclature_sid AS sid,
                    gn.goods_nomenclature_item_id AS code,
                    st.self_text,
                    st.search_text,
                    st.generation_type,
                    ts_rank(
                        to_tsvector('english', COALESCE(st.search_text, st.self_text)),
                        websearch_to_tsquery('english', $1)
                    ) AS score
                FROM uk.goods_nomenclature_self_texts st
                JOIN uk.goods_nomenclatures gn USING (goods_nomenclature_sid)
                WHERE to_tsvector('english', COALESCE(st.search_text, st.self_text))
                      @@ websearch_to_tsquery('english', $1)
                  AND gn.producline_suffix = $2
                  AND gn.goods_nomenclature_item_id NOT IN (
                      SELECT goods_nomenclature_item_id FROM uk.hidden_goods_nomenclatures
                  )
                ORDER BY score DESC
                LIMIT $3
                """,
                query,
                NON_GROUPING_PRODUCTLINE_SUFFIX,
                over_fetch,
            )
        return [
            Candidate(
                sid=r["sid"], code=r["code"],
                self_text=r["self_text"], search_text=r["search_text"],
                generation_type=r["generation_type"],
                score=0.0, bm25_rank=rank + 1,
            )
            for rank, r in enumerate(rows)
        ]

    def rrf_merge(
        self,
        vector_items: list[Candidate],
        keyword_items: list[Candidate],
        k: int = RRF_K,
    ) -> list[Candidate]:
        """Reciprocal Rank Fusion, identical to HybridRetrievalService#rrf_merge."""
        scores: dict[int, float] = {}
        by_sid: dict[int, Candidate] = {}
        bm25_rank_by_sid: dict[int, int] = {}
        vector_rank_by_sid: dict[int, int] = {}
        cosine_by_sid: dict[int, float] = {}

        for rank, c in enumerate(vector_items):
            scores[c.sid] = scores.get(c.sid, 0.0) + 1.0 / (rank + 1 + k)
            by_sid.setdefault(c.sid, c)
            vector_rank_by_sid[c.sid] = rank + 1
            if c.cosine_score is not None:
                cosine_by_sid[c.sid] = c.cosine_score

        for rank, c in enumerate(keyword_items):
            scores[c.sid] = scores.get(c.sid, 0.0) + 1.0 / (rank + 1 + k)
            by_sid.setdefault(c.sid, c)
            bm25_rank_by_sid[c.sid] = rank + 1

        out: list[Candidate] = []
        for sid, sc in sorted(scores.items(), key=lambda kv: -kv[1]):
            c = by_sid[sid]
            c.score = sc
            c.bm25_rank = bm25_rank_by_sid.get(sid)
            c.vector_rank = vector_rank_by_sid.get(sid)
            c.cosine_score = cosine_by_sid.get(sid)
            c.declarable = sid in (self._declarable_sids or set())
            out.append(c)
        return out

    async def retrieve(
        self,
        query: str,
        limit: int = 30,
        vector_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Top-level entry point. Returns fused, declarable-filtered candidates.

        vector_threshold: override the cosine-similarity floor for the vector
        leg. None uses production default (0.35). 0.0 disables the filter.
        """
        # Allow per-call threshold override by temporarily swapping the module
        # constant for the vector_leg call. Not threadsafe but the FastAPI
        # endpoint is single-threaded per request.
        global VECTOR_SCORE_THRESHOLD
        prev_threshold = VECTOR_SCORE_THRESHOLD
        try:
            if vector_threshold is not None:
                VECTOR_SCORE_THRESHOLD = float(vector_threshold)
            embedding = await self.embed(query)
            vec_task = asyncio.create_task(self.vector_leg(embedding, over_fetch=limit))
            kw_task = asyncio.create_task(self.keyword_leg(query, over_fetch=limit))
            vec, kw = await asyncio.gather(vec_task, kw_task)
            below = list(getattr(self, "_last_below_threshold", []) or [])
        finally:
            VECTOR_SCORE_THRESHOLD = prev_threshold

        fused = self.rrf_merge(vec, kw)
        declarable_only = [c for c in fused if c.declarable]
        return {
            "query": query,
            "vector_count": len(vec),
            "keyword_count": len(kw),
            "fused_count": len(fused),
            "declarable_count": len(declarable_only),
            "candidates": [c.to_dict() for c in declarable_only[:limit]],
            "vector_threshold_used": vector_threshold if vector_threshold is not None else prev_threshold,
            "below_threshold_candidates": below,
        }
