"""Mirror production's CompositeSearchTextBuilder in the POC corpus.

Production indexes each commodity's SEARCH text as:
    <self_text>
    Also known as: <colloquial_terms + synonyms>   (AI-166 labels)
    Brands: <known_brands>                          (AI-166 labels)
    References: <search_reference titles>
The trader-journey-poc retrieval used self_text ALONE, so AI-166 trader-vocabulary
(colloquial/synonyms/brands) never reached retrieval - which is why "Other"/residual
codes missed. This seeder builds the composite per code and embeds it into
kg.composite_search_text so the vector + FTS legs can search the same text production does.

v1 simplification: own search_references only (production also folds in ancestors'
references); the dominant signal is the AI-166 labels, which we include in full.

Cost: ~25k text-embedding-3-small calls (~$0.10). Resumable via the `stale` flag.
Env: COMPOSITE_BATCH(256) COMPOSITE_FORCE(0)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

for p in [
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
]:
    if p is not None and p.exists():
        load_dotenv(p)
        break

import psycopg
from psycopg.rows import dict_row

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MODEL = "text-embedding-3-small"
BATCH = int(os.environ.get("COMPOSITE_BATCH", "256"))
FORCE = os.environ.get("COMPOSITE_FORCE", "0") == "1"


def build_composite(self_text, colloquial, synonyms, brands, refs) -> str:
    """Mirror CompositeSearchTextBuilder.call()."""
    sections = [self_text or ""]
    aka = [*(colloquial or []), *(synonyms or [])]
    aka = [a for a in aka if a and a.strip()]
    if aka:
        sections.append("Also known as: " + ", ".join(aka))
    br = [b for b in (brands or []) if b and b.strip()]
    if br:
        sections.append("Brands: " + ", ".join(br))
    rf = [r for r in (refs or []) if r and r.strip()]
    if rf:
        sections.append("References: " + ", ".join(sorted(set(rf))))
    return "\n".join(sections)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kg.composite_search_text (
                goods_nomenclature_item_id text PRIMARY KEY,
                composite_text text NOT NULL,
                composite_embedding vector(1536),
                stale boolean NOT NULL DEFAULT true
            )""")
    conn.commit()


def populate_text(conn) -> int:
    """Build composite_text for every code with a self_text (idempotent upsert).
    Marks rows stale=true when the text changed so embeddings get refreshed."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH lab AS (
              SELECT goods_nomenclature_item_id code, colloquial_terms, synonyms, known_brands
              FROM uk.goods_nomenclature_labels),
            ref AS (
              SELECT goods_nomenclature_item_id code, array_agg(DISTINCT title) titles
              FROM uk.search_references WHERE title IS NOT NULL GROUP BY 1)
            SELECT st.goods_nomenclature_item_id code, st.self_text,
                   lab.colloquial_terms, lab.synonyms, lab.known_brands, ref.titles
            FROM uk.goods_nomenclature_self_texts st
            LEFT JOIN lab ON lab.code = st.goods_nomenclature_item_id
            LEFT JOIN ref ON ref.code = st.goods_nomenclature_item_id
            WHERE st.self_text IS NOT NULL
        """)
        rows = cur.fetchall()
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            comp = build_composite(r["self_text"], r["colloquial_terms"], r["synonyms"],
                                   r["known_brands"], r["titles"])
            cur.execute("""
                INSERT INTO kg.composite_search_text (goods_nomenclature_item_id, composite_text, stale)
                VALUES (%s,%s,true)
                ON CONFLICT (goods_nomenclature_item_id) DO UPDATE
                  SET composite_text=EXCLUDED.composite_text,
                      stale = (kg.composite_search_text.composite_text IS DISTINCT FROM EXCLUDED.composite_text
                               OR kg.composite_search_text.composite_embedding IS NULL)
            """, (r["code"], comp))
            n += 1
    conn.commit()
    return n


def embed_stale(conn) -> int:
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=60.0, max_retries=4)
    with conn.cursor() as cur:
        cur.execute("SELECT goods_nomenclature_item_id code, composite_text FROM kg.composite_search_text "
                    + ("" if FORCE else "WHERE stale OR composite_embedding IS NULL"))
        todo = cur.fetchall()
    print(f"  embedding {len(todo)} composite texts...", flush=True)
    done = 0
    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        resp = client.embeddings.create(model=MODEL, input=[c["composite_text"][:8000] for c in chunk])
        with conn.cursor() as cur:
            for c, e in zip(chunk, resp.data):
                vec = "[" + ",".join(f"{x:.6f}" for x in e.embedding) + "]"
                cur.execute("UPDATE kg.composite_search_text SET composite_embedding=%s::vector, stale=false "
                            "WHERE goods_nomenclature_item_id=%s", (vec, c["code"]))
        conn.commit()
        done += len(chunk)
        if (i // BATCH) % 10 == 0:
            print(f"    {done}/{len(todo)} ({time.time()-t0:.0f}s)", flush=True)
    return done


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required", file=sys.stderr); sys.exit(1)
    conn = psycopg.connect(DSN, row_factory=dict_row)
    ensure_schema(conn)
    n = populate_text(conn)
    print(f"composite_text populated for {n} codes")
    e = embed_stale(conn)
    print(f"embedded {e} composite texts -> kg.composite_search_text")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) n, count(composite_embedding) emb FROM kg.composite_search_text")
        r = cur.fetchone(); print(f"total rows {r['n']}, embedded {r['emb']}")
    conn.close()


if __name__ == "__main__":
    main()
