"""Parallel LLM facet extraction across all uncovered codes in Ch 22/64/73.

Runs N concurrent OpenAI calls (asyncio + AsyncOpenAI). Cuts wall-clock from
~2h down to ~10-15 min for the full ~1400-code chapter sweep.
"""
from __future__ import annotations

import asyncio
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

import asyncpg
from openai import AsyncOpenAI
try:
    from .evidence_labels import facet_labels
except ImportError:
    from evidence_labels import facet_labels

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
LLM_MODEL = os.environ.get("SEED_LLM_MODEL", "gpt-5.5")
SEED_CHAPTERS = ["22", "64", "73"]
CONCURRENCY = int(os.environ.get("SEED_CONCURRENCY", "8"))

# OpenAI pricing (per 1M tokens, USD). gpt-5.5 published pricing.
COST_PER_M_INPUT = float(os.environ.get("LLM_INPUT_COST_PER_M", "2.50"))
COST_PER_M_OUTPUT = float(os.environ.get("LLM_OUTPUT_COST_PER_M", "15.00"))

# Mutable token counters (single async loop = thread-safe enough).
_total_prompt_tokens = 0
_total_completion_tokens = 0


EXTRACTION_PROMPT = """You are extracting structured commodity facts from a UK tariff description.

Return JSON of the form `{"facts": [{"slot": "...", "value": "..."}, ...]}`.

Slot guidelines:
- snake_case slot names (e.g. material_upper, beverage_type, container_size)
- short values (1-3 words)
- 4-10 facts per commodity, only ones clearly stated
- Use values useful as multiple-choice answers when narrowing similar codes
- Don't invent. If description is short, return fewer facts.

Example:
{"facts":[
  {"slot":"material_upper","value":"rubber_or_plastic"},
  {"slot":"closure","value":"strap_thong"}
]}"""


async def extract_one(client: AsyncOpenAI, code: str, description: str, self_text: str | None) -> list[dict]:
    user = f"Commodity: {code}\nDescription: {description}\n"
    if self_text:
        user += f"\nAdditional text: {self_text[:400]}"
    kwargs: dict = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    if LLM_MODEL.startswith("gpt-5"):
        kwargs["max_completion_tokens"] = 800
    else:
        kwargs["max_tokens"] = 400
        kwargs["temperature"] = 0.0
    global _total_prompt_tokens, _total_completion_tokens
    try:
        r = await client.chat.completions.create(**kwargs)
        if r.usage:
            _total_prompt_tokens += r.usage.prompt_tokens
            _total_completion_tokens += r.usage.completion_tokens
        text = (r.choices[0].message.content or "").strip()
        d = json.loads(text)
        facts = d.get("facts", []) if isinstance(d, dict) else []
        return [
            {"slot": f.get("slot"), "value": str(f.get("value"))}
            for f in facts
            if isinstance(f, dict) and f.get("slot") and f.get("value") is not None
        ]
    except Exception as e:
        print(f"  ! extract {code}: {type(e).__name__}: {e}")
        return []


async def process_code(
    client: AsyncOpenAI,
    pool: asyncpg.Pool,
    code: str,
    description: str,
    self_text: str | None,
    sem: asyncio.Semaphore,
) -> int:
    async with sem:
        facts = await extract_one(client, code, description, self_text)
    if not facts:
        return 0
    async with pool.acquire() as c:
        async with c.transaction():
            n = 0
            for f in facts:
                slot = f["slot"]
                value = f["value"]
                if not slot or value in (None, "", "unknown", "Unknown"):
                    continue
                use_scopes, evidence_roles = facet_labels("description_llm", slot, value)
                await c.execute(
                    "INSERT INTO kg.facet_definitions (key, label, applies_to_chapters) "
                    "VALUES ($1, $2, ARRAY[$3]::text[]) ON CONFLICT (key) DO UPDATE SET "
                    "applies_to_chapters = (SELECT array_agg(DISTINCT x) FROM unnest(kg.facet_definitions.applies_to_chapters || ARRAY[$3]::text[]) x)",
                    slot, slot.replace("_", " ").capitalize(), code[:2],
                )
                await c.execute(
                    """
                    INSERT INTO kg.commodity_facets
                      (commodity_code, facet_key, facet_value, source, confidence, evidence, authority_tier, use_scopes, evidence_roles, provenance)
                    VALUES ($1, $2, $3, 'description_llm', 0.85, $4, 6, $5::text[], $6::text[], $7::jsonb)
                    ON CONFLICT (commodity_code, facet_key, facet_value, source) DO NOTHING
                    """,
                    code,
                    slot,
                    value,
                    description[:500],
                    use_scopes,
                    evidence_roles,
                    json.dumps({
                        "source_type": "tariff_description",
                        "extractor": "seed_facets_parallel",
                        "extractor_model": LLM_MODEL,
                    }),
                )
                n += 1
            return n


async def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("no OPENAI_API_KEY; aborting")
        return
    client = AsyncOpenAI(api_key=api_key)
    pool = await asyncpg.create_pool(DSN, min_size=2, max_size=CONCURRENCY + 2, command_timeout=60)
    sem = asyncio.Semaphore(CONCURRENCY)

    # Pull all uncovered codes for the target chapters
    async with pool.acquire() as c:
        rows = await c.fetch(
            """
            SELECT DISTINCT ON (gn.goods_nomenclature_item_id)
                   gn.goods_nomenclature_item_id AS code,
                   gnd.description AS description,
                   st.self_text AS self_text
            FROM uk.goods_nomenclatures gn
            JOIN uk.goods_nomenclature_descriptions gnd
              ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
            LEFT JOIN uk.goods_nomenclature_self_texts st
              ON st.goods_nomenclature_item_id = gn.goods_nomenclature_item_id
            WHERE gn.validity_end_date IS NULL
              AND LEFT(gn.goods_nomenclature_item_id, 2) = ANY($1)
              AND gnd.description IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM kg.commodity_facets cf
                WHERE cf.commodity_code = gn.goods_nomenclature_item_id
              )
            ORDER BY gn.goods_nomenclature_item_id
            """,
            SEED_CHAPTERS,
        )
    print(f"to process: {len(rows)} codes (concurrency={CONCURRENCY}, model={LLM_MODEL})")

    tasks = [
        asyncio.create_task(process_code(client, pool, r["code"], r["description"], r["self_text"], sem))
        for r in rows
    ]
    done = 0
    facts_total = 0
    for fut in asyncio.as_completed(tasks):
        n = await fut
        done += 1
        facts_total += n
        if done % 25 == 0 or done == len(tasks):
            print(f"  {done}/{len(tasks)} - facts so far: {facts_total}")
    cost = (_total_prompt_tokens * COST_PER_M_INPUT + _total_completion_tokens * COST_PER_M_OUTPUT) / 1_000_000
    print(f"\nDone. Processed {len(tasks)} codes, inserted {facts_total} facts.")
    print(f"Tokens: prompt={_total_prompt_tokens:,} completion={_total_completion_tokens:,}")
    print(f"Cost (this run): ${cost:.4f} at gpt-5.5 rates")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
