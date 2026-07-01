"""Facet enrichment: LLM-extract structured facets from the official goods-nomenclature
description for eval codes that lack `description_llm` facets (mirrors what seed_facts_kg
does for the seeded chapters, but scoped to the eval corpus). Idempotent.

Run: .venv/bin/python -m journey.enrich_facets   (env: FACET_LLM_MODEL, FACET_CONCURRENCY)
"""
import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

for _p in [
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
]:
    if _p is not None and _p.exists():
        load_dotenv(_p)
        break

import psycopg
from psycopg.rows import dict_row
from openai import AsyncOpenAI
try:
    from .evidence_labels import facet_labels
except ImportError:
    from evidence_labels import facet_labels

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MODEL = os.environ.get("FACET_LLM_MODEL", "gpt-5-mini")
CONC = int(os.environ.get("FACET_CONCURRENCY", "6"))

PROMPT = (
    "You extract structured classification facets for a UK tariff commodity code from its "
    "official description. Reply JSON only: "
    '{"facets":[{"key":"<one of: material, form, function, intended_use, processing_state, distinguishing_feature>","value":"<short phrase>"}]}. '
    "5-10 facets. Only facts implied by the description; never guess."
)


def targets():
    conn = psycopg.connect(DSN, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("""
          SELECT DISTINCT g.expected_code AS code,
            (SELECT d.description FROM uk.goods_nomenclature_descriptions d
             JOIN uk.goods_nomenclatures gn ON gn.goods_nomenclature_sid = d.goods_nomenclature_sid
             WHERE gn.goods_nomenclature_item_id = g.expected_code
               AND gn.validity_end_date IS NULL AND d.language_id='EN' LIMIT 1) AS descr
          FROM kg.eval_gold g
          WHERE g.expected_code NOT IN (
            SELECT commodity_code FROM kg.commodity_facets WHERE source='description_llm')
        """)
        rows = [r for r in cur.fetchall() if r["descr"]]
    conn.close()
    return rows


async def one(client, row, sem):
    async with sem:
        try:
            resp = await asyncio.wait_for(client.chat.completions.create(
                model=MODEL, reasoning_effort="minimal",
                messages=[{"role": "system", "content": PROMPT},
                          {"role": "user", "content": f"Code {row['code']}: {row['descr'][:1500]}"}],
                response_format={"type": "json_object"}), timeout=60)
            facets = json.loads(resp.choices[0].message.content or "{}").get("facets", [])
        except Exception as e:
            print(f"  [{row['code']}] {e!r}")
            return 0
        n = 0
        conn = psycopg.connect(DSN)
        try:
            with conn.cursor() as cur:
                for f in facets:
                    k = str(f.get("key", "")).strip()
                    v = str(f.get("value", "")).strip()
                    if not k or not v:
                        continue
                    use_scopes, evidence_roles = facet_labels("description_llm", k, v)
                    cur.execute("INSERT INTO kg.facet_definitions (key,label) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING",
                                (k, k.replace("_", " ").capitalize()))
                    cur.execute("""INSERT INTO kg.commodity_facets
                                     (commodity_code,facet_key,facet_value,source,confidence,evidence,authority_tier,use_scopes,evidence_roles,provenance)
                                   VALUES (%s,%s,%s,'description_llm',0.8,%s,6,%s::text[],%s::text[],%s::jsonb)
                                   ON CONFLICT (commodity_code,facet_key,facet_value,source) DO NOTHING""",
                                (
                                    row["code"], k, v, row["descr"][:300],
                                    use_scopes, evidence_roles,
                                    json.dumps({
                                        "source_type": "tariff_description",
                                        "extractor": "enrich_facets",
                                        "extractor_model": MODEL,
                                    }),
                                ))
                    n += 1
            conn.commit()
        finally:
            conn.close()
        return n


async def main():
    rows = targets()
    print(f"enriching {len(rows)} eval codes lacking description_llm facets | model={MODEL}")
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    sem = asyncio.Semaphore(CONC)
    res = await asyncio.gather(*[one(client, r, sem) for r in rows])
    print(f"DONE: inserted {sum(res)} facets across {len([x for x in res if x])} codes")


if __name__ == "__main__":
    asyncio.run(main())
