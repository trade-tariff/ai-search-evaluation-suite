"""V2 paraphraser: re-use fan-out's battle-tested trader emulator from
ai-fan-out/backend/intercepts.py (`_TIERED_SYSTEM` + `_is_acceptable_query`).

That module has:
  - tighter prompt rules (no n.e.s., no chapter numbers, no CAS, no denier...)
  - a post-filter regex that rejects jargon-leaking queries
  - a 3-tier output (generic / ordinary / specific) close to our personas

We add THREE new rows per ATAR in kg.eval_gold with personas:
  - emu_generic   (1-3 words)
  - emu_ordinary  (2-6 words)
  - emu_specific  (4-10 words)

Different persona names from v1 (naive_vague / naive_branded / naive_specific)
so we can compare paraphraser quality directly in the eval harness.

Cost: gpt-5-mini, ~1k tokens per ATAR x 70 = ~70k tokens = ~$0.01.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

for p in [Path(__file__).parent / ".env",
          Path(__file__).parent.parent / ".env",
          Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row
from openai import AsyncOpenAI

# Pull in the fan-out emulator. Direct path import so we don't need to install
# either project as a package.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from intercepts import generate_user_queries_tiered, PARAPHRASE_MODEL  # noqa: E402

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
CONCURRENCY = int(os.environ.get("PARA_CONCURRENCY", "6"))


def get_atars() -> list[dict]:
    conn = psycopg.connect(DSN, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.body, e.title, kec.commodity_code,
                   (SELECT description FROM uk.goods_nomenclature_descriptions gnd
                    JOIN uk.goods_nomenclatures gn ON gn.goods_nomenclature_sid = gnd.goods_nomenclature_sid
                    WHERE gn.goods_nomenclature_item_id = kec.commodity_code
                      AND gn.validity_end_date IS NULL LIMIT 1) AS expected_description
            FROM kg.kg_edges e
            JOIN kg.kg_edge_commodities kec ON kec.edge_id = e.id
            WHERE e.id LIKE 'atar_%'
              -- guard: skip ATARs that already have all 3 emu personas, so re-runs
              -- only fill ATARs still MISSING emu (no row inflation on re-run).
              AND e.id NOT IN (
                SELECT source_id FROM kg.eval_gold
                WHERE persona LIKE 'emu%'
                GROUP BY source_id HAVING count(DISTINCT persona) = 3
              )
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def insert_gold(source_id: str, persona: str, query: str, expected_code: str,
                expected_description: str | None) -> bool:
    conn = psycopg.connect(DSN, row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kg.eval_gold
                  (source_type, source_id, persona, query, expected_code, expected_description, notes, generator)
                VALUES ('atar', %s, %s, %s, %s, %s, 'fanout tiered emulator', %s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (source_id, persona, query.strip().lower(), expected_code, expected_description, PARAPHRASE_MODEL),
            )
            return cur.fetchone() is not None
    finally:
        conn.commit()
        conn.close()


async def process_atar(client: AsyncOpenAI, atar: dict, sem: asyncio.Semaphore) -> int:
    async with sem:
        body = atar.get("body") or atar.get("title") or ""
        if not body.strip():
            return 0
        tiers = await generate_user_queries_tiered(client, body[:1500])
        if not tiers:
            print(f"  [{atar['id']}] emulator returned None")
            return 0
        inserted = 0
        persona_map = [("emu_generic", "generic"), ("emu_ordinary", "ordinary"), ("emu_specific", "specific")]
        for persona, tier_key in persona_map:
            if insert_gold(atar["id"], persona, tiers[tier_key], atar["commodity_code"], atar.get("expected_description")):
                inserted += 1
        return inserted


async def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    client = AsyncOpenAI(api_key=api_key)
    atars = get_atars()
    print(f"ATARs: {len(atars)} | model: {PARAPHRASE_MODEL} | concurrency: {CONCURRENCY}")

    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*[process_atar(client, a, sem) for a in atars])
    total = sum(results)
    print(f"\nInserted {total} new gold rows (emu_generic / emu_ordinary / emu_specific)")

    conn = psycopg.connect(DSN, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SELECT persona, COUNT(*) FROM kg.eval_gold GROUP BY 1 ORDER BY 1")
        for r in cur.fetchall():
            print(f"  {r['persona']}: {r['count']}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
