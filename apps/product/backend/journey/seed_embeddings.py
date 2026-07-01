"""Embed facts and KG edges so retrieval can do semantic matching.

Terminology: each kg.commodity_facets row is a FACT - one code's value on a
facet axis ("material_upper: rubber"). The FACET is the facet_key; questions
are asked about facets, and the answer options are the facts present on that
facet across the candidate shortlist. (The table name predates this usage.)

Per-fact, per-edge embeddings (NOT per-code aggregates). This matches codex's
recommendation: per-code bags mix unrelated facts and lose attribution. With
per-fact embeddings we can tell the user "fact X about CITES matched your query",
and we can aggregate to commodity codes via RRF/max at fusion time.

Embedding model: text-embedding-3-small (1536 dim) - same as fan-out's
goods_nomenclature_self_texts.search_embedding so dimensions line up.

Idempotent: only embeds rows where embedding_stale = true. Trigger in
003_fact_kg_embeddings.sql flips the flag back on whenever the source text
changes.

Cost: text-embedding-3-small is $0.02/M tokens. ~16k facts * ~30 tokens avg
= ~500k tokens = ~$0.01. Negligible. Just re-run after every seeder change.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env from anywhere reasonable
for envp in [
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
    Path(__file__).parent.parent.parent / ".env",
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
]:
    if envp is not None and envp.exists():
        load_dotenv(envp)
        break

import asyncpg
from openai import AsyncOpenAI

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MODEL = "text-embedding-3-small"
BATCH_SIZE = int(os.environ.get("EMBED_BATCH", "128"))
CONCURRENCY = int(os.environ.get("EMBED_CONCURRENCY", "4"))
EXCLUDE_FACT_SOURCES = [
    item.strip()
    for item in os.environ.get(
        "EMBED_EXCLUDE_FACT_SOURCES",
        "goods_nomenclature_labels,search_reference",
    ).split(",")
    if item.strip()
]

_total_tokens = 0
_total_embedded = 0


def _fact_text(facet_key: str, facet_value: str, evidence: str | None) -> str:
    """Build the string we embed for a fact row.

    The trader vocabulary -> tariff vocabulary gap is exactly what semantic
    retrieval is supposed to bridge. So we lead with the human-readable bit
    (evidence, which we enriched in seed_extra_sources.py) and use the
    structured fields as keyword anchors.
    """
    parts = []
    if evidence:
        parts.append(evidence.strip())
    parts.append(f"{facet_key}: {facet_value}")
    return " | ".join(parts)


def _edge_text(title: str, body: str) -> str:
    """OpenAI embedding cap is 8192 tokens. We truncate to ~30k chars (~7500
    tokens) to stay safely under. HSEN edges in particular can be 30k+ chars.

    Future improvement: chunk long edges into multiple per-edge segments and
    aggregate. For the POC, lossy truncation of the tail is acceptable: the
    title + opening paragraphs of an HSEN edge carry most of the semantic
    weight for retrieval.
    """
    title = (title or "").strip()
    body = (body or "").strip()
    combined = f"{title}\n{body}" if (title and body) else (title or body)
    if len(combined) > 24000:
        combined = combined[:24000]
    return combined


async def _embed_batch(client: AsyncOpenAI, texts: list[str]) -> tuple[list[list[float]], int]:
    """Embed a batch and return (vectors, tokens_used)."""
    resp = await client.embeddings.create(model=MODEL, input=texts)
    tokens = resp.usage.total_tokens if resp.usage else 0
    return [d.embedding for d in resp.data], tokens


async def embed_facts(pool: asyncpg.Pool, client: AsyncOpenAI) -> int:
    """Stream stale facts in batches, embed, write back."""
    global _total_tokens, _total_embedded
    print("=" * 60)
    print("1. Embedding stale facts in kg.commodity_facets")
    if EXCLUDE_FACT_SOURCES:
        print(f"  excluding fact sources: {', '.join(EXCLUDE_FACT_SOURCES)}")
    sem = asyncio.Semaphore(CONCURRENCY)
    embedded = 0
    while True:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, facet_key, facet_value, evidence
                FROM kg.commodity_facets
                WHERE embedding_stale = true
                  AND ( $2::text[] IS NULL OR source <> ALL($2::text[]) )
                ORDER BY id
                LIMIT $1
                """,
                BATCH_SIZE * CONCURRENCY,
                EXCLUDE_FACT_SOURCES or None,
            )
        if not rows:
            break
        # Split into CONCURRENCY sub-batches and embed in parallel
        sub_batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]

        async def process(batch):
            async with sem:
                texts = [_fact_text(r["facet_key"], r["facet_value"], r["evidence"]) for r in batch]
                vecs, tokens = await _embed_batch(client, texts)
                return batch, vecs, tokens

        results = await asyncio.gather(*[process(b) for b in sub_batches])

        # Write back. Use a single transaction per sub-batch.
        async with pool.acquire() as conn:
            async with conn.transaction():
                for batch, vecs, tokens in results:
                    _total_tokens += tokens
                    for r, v in zip(batch, vecs):
                        # Disable the trigger temporarily via session_replication_role
                        # would be heavy; instead just set the columns directly. The
                        # trigger only fires if facet_key/value/evidence changes.
                        await conn.execute(
                            "UPDATE kg.commodity_facets SET embedding = $1::vector, embedding_stale = false WHERE id = $2",
                            "[" + ",".join(str(x) for x in v) + "]",
                            r["id"],
                        )
                        embedded += 1
                        _total_embedded += 1
        print(f"  embedded {embedded} so far (~{_total_tokens} tokens, ~${_total_tokens * 0.00000002:.4f})")
    print(f"  done facts: {embedded} embedded this run")
    return embedded


async def embed_edges(pool: asyncpg.Pool, client: AsyncOpenAI) -> int:
    """Same shape as embed_facts but for kg.kg_edges."""
    global _total_tokens, _total_embedded
    print("=" * 60)
    print("2. Embedding stale edges in kg.kg_edges")
    sem = asyncio.Semaphore(CONCURRENCY)
    embedded = 0
    while True:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, title, body
                FROM kg.kg_edges
                WHERE embedding_stale = true
                ORDER BY id
                LIMIT $1
                """,
                BATCH_SIZE * CONCURRENCY,
            )
        if not rows:
            break
        sub_batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]

        async def process(batch):
            async with sem:
                texts = [_edge_text(r["title"], r["body"]) for r in batch]
                vecs, tokens = await _embed_batch(client, texts)
                return batch, vecs, tokens

        results = await asyncio.gather(*[process(b) for b in sub_batches])
        async with pool.acquire() as conn:
            async with conn.transaction():
                for batch, vecs, tokens in results:
                    _total_tokens += tokens
                    for r, v in zip(batch, vecs):
                        await conn.execute(
                            "UPDATE kg.kg_edges SET embedding = $1::vector, embedding_stale = false WHERE id = $2",
                            "[" + ",".join(str(x) for x in v) + "]",
                            r["id"],
                        )
                        embedded += 1
                        _total_embedded += 1
        print(f"  embedded {embedded} edges so far")
    print(f"  done edges: {embedded} embedded this run")
    return embedded


async def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    client = AsyncOpenAI(api_key=api_key)
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=CONCURRENCY + 2)
    start = time.time()
    try:
        await embed_facts(pool, client)
        await embed_edges(pool, client)
    finally:
        await pool.close()
    elapsed = time.time() - start
    cost = _total_tokens * 0.00000002  # text-embedding-3-small price
    print(f"\nTotal: {_total_embedded} rows embedded, {_total_tokens} tokens, ~${cost:.4f}, {elapsed:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
