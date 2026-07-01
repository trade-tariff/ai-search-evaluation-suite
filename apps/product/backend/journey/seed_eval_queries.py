"""Generate naive-trader paraphrases of ATAR product descriptions for the
retrieval eval harness.

Per ATAR (we have 70 in the KG, each linked 1:1 to a commodity code), generate
4 rows in kg.eval_gold:
  - original:        verbatim ATAR description (long, customs-language baseline)
  - naive_vague:     2-5 word generalist guess  ("metal bracket", "shoes")
  - naive_branded:   colloquial / brand / market language ("Crocs", "Nespresso pod")
  - naive_specific:  novice attempt at precision (partly correct details)

Cost: ~70 ATARs x 1 LLM call (returns 3 paraphrases) x ~500 tokens
    = ~$0.10-0.20 with gpt-5.5.

Re-runnable: ON CONFLICT skips existing rows.
"""
from __future__ import annotations

import json
import os
import sys
import time
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
from openai import OpenAI

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MODEL = os.environ.get("EVAL_LLM_MODEL", "gpt-5.5")

PARAPHRASE_SYSTEM = """You generate input queries to test a UK commodity-code classifier.

Your job: take a verbose customs-language product description and produce 3 different ways a NAIVE SMALL-BUSINESS IMPORTER might type the same product into a search bar. They have NO customs training. They might be vague, use brand or market names, or get a technical term slightly wrong.

Return EXACTLY this JSON:
{
  "naive_vague":   "2-5 word generalist guess, no technical terms",
  "naive_branded": "colloquial/market/brand language, 3-8 words",
  "naive_specific": "novice attempt at being precise, 5-12 words. May get one detail wrong."
}

Rules:
- Each variant must describe the SAME product the original describes.
- Lower case unless brand name needs capital.
- No commas, no semicolons - traders just type a search.
- naive_vague: 2-5 words MAX. Strip detail. e.g. "shoes", "metal bracket", "plastic mug".
- naive_branded: think how a small importer would describe it on Amazon. Use brand names if relevant (e.g. "Crocs", "Nespresso pod", "GoPro").
- naive_specific: 5-12 words. Try to be precise but expect a non-expert level of detail. Maybe say "rubber" instead of "elastomer", "for kitchen" instead of "for catering".
- Do NOT include the commodity code or chapter number.
"""


def get_atars(conn) -> list[dict]:
    """Pull all ATARs with their linked commodity code and product description."""
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
            """
        )
        return [dict(r) for r in cur.fetchall()]


def insert_gold(conn, source_type: str, source_id: str, persona: str, query: str,
                expected_code: str, expected_description: str | None,
                notes: str | None, generator: str) -> bool:
    """Returns True if inserted, False if conflict."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kg.eval_gold
              (source_type, source_id, persona, query, expected_code, expected_description, notes, generator)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            (source_type, source_id, persona, query.strip().lower(), expected_code, expected_description, notes, generator),
        )
        return cur.fetchone() is not None


def paraphrase_one(client: OpenAI, body: str, code: str) -> dict | None:
    """Returns {naive_vague, naive_branded, naive_specific} or None on failure."""
    # Cap body length to avoid burning input tokens on huge ATARs
    body_trimmed = body[:2500]
    user = f"Product description (target commodity code: {code}):\n\n{body_trimmed}"
    try:
        kwargs = {"model": MODEL, "messages": [
            {"role": "system", "content": PARAPHRASE_SYSTEM},
            {"role": "user", "content": user},
        ], "response_format": {"type": "json_object"}}
        # gpt-5.x uses max_completion_tokens; older models use max_tokens.
        if MODEL.startswith("gpt-5"):
            kwargs["max_completion_tokens"] = 400
        else:
            kwargs["max_tokens"] = 400
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        if not content:
            return None
        data = json.loads(content)
        # Basic sanity: 3 keys, each non-empty
        keys = ("naive_vague", "naive_branded", "naive_specific")
        if not all(k in data and isinstance(data[k], str) and data[k].strip() for k in keys):
            return None
        return {k: data[k].strip() for k in keys}
    except Exception as exc:
        print(f"  [LLM error] {type(exc).__name__}: {exc}")
        return None


def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    client = OpenAI(api_key=api_key)
    conn = psycopg.connect(DSN, row_factory=dict_row)

    atars = get_atars(conn)
    print(f"ATARs to process: {len(atars)}")

    n_atars_processed = 0
    n_rows_inserted = 0
    n_skipped_existing = 0
    start = time.time()

    for atar in atars:
        atar_id = atar["id"]
        code = atar["commodity_code"]
        body = atar["body"] or atar["title"] or ""
        desc = atar.get("expected_description")
        if not body.strip():
            continue

        # Persona: original (verbatim - sanity baseline). Always inserted.
        ok = insert_gold(conn, "atar", atar_id, "original",
                         body[:500],  # cap so we don't store 5kb queries
                         code, desc, "verbatim ATAR product description", "verbatim")
        conn.commit()
        if ok:
            n_rows_inserted += 1
        else:
            n_skipped_existing += 1

        # Check if naive personas already exist for this ATAR; skip LLM if so.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM kg.eval_gold WHERE source_id = %s AND persona LIKE 'naive_%%'",
                (atar_id,),
            )
            existing_naive = cur.fetchone()["n"]
        if existing_naive >= 3:
            n_atars_processed += 1
            continue

        # Generate the 3 naive paraphrases
        p = paraphrase_one(client, body, code)
        if not p:
            print(f"  [{atar_id}] LLM failed, skipping")
            continue
        for persona, query in [("naive_vague", p["naive_vague"]),
                               ("naive_branded", p["naive_branded"]),
                               ("naive_specific", p["naive_specific"])]:
            ok = insert_gold(conn, "atar", atar_id, persona, query, code, desc,
                             f"paraphrased ATAR {atar_id}", MODEL)
            if ok:
                n_rows_inserted += 1
            else:
                n_skipped_existing += 1
        conn.commit()
        n_atars_processed += 1
        if n_atars_processed % 10 == 0:
            print(f"  {n_atars_processed}/{len(atars)} ATARs done, {n_rows_inserted} new gold rows")

    elapsed = time.time() - start
    print(f"\nDone: {n_atars_processed} ATARs, {n_rows_inserted} new gold rows, "
          f"{n_skipped_existing} already existed, {elapsed:.1f}s")
    with conn.cursor() as cur:
        cur.execute("SELECT persona, COUNT(*) FROM kg.eval_gold GROUP BY 1 ORDER BY 1")
        for r in cur.fetchall():
            print(f"  {r['persona']}: {r['count']}")
    conn.close()


if __name__ == "__main__":
    main()
