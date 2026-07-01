"""Deeper ATAR ingest: paginate the FULL ATAR listing (no keyword filter) to surface
the long tail of DISTINCT commodity codes. The keyword approach saturated at 64
distinct codes (searches return the same popular rulings); the full listing has
~7,024 rulings spanning far more distinct codes.

The listing page shows each ruling's commodity code inline, so the SCAN is cheap
(parse codes from listing pages, no per-ruling fetch). We collect the long-tail
NEW codes (not already in eval_gold) and only fetch + LLM-extract facts for those
keepers. Early listing pages overlap with the popular codes we already have, so
new codes appear deeper in the listing.

Run: .venv/bin/python -m journey.seed_atars_full_listing
Env: TARGET_NEW_CODES (50), ATAR_MAX_PAGES (281), ATAR_START_PAGE (1)
"""
import os
import re
import time

import httpx
import psycopg
from psycopg.rows import dict_row

from .seed_atars_for_chapters import (
    ATAR_BASE, fetch_ruling, llm_extract_facts, upsert_atar, _flat,
)

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
TARGET_NEW_CODES = int(os.environ.get("TARGET_NEW_CODES", "50"))
MAX_PAGES = int(os.environ.get("ATAR_MAX_PAGES", "281"))
START_PAGE = int(os.environ.get("ATAR_START_PAGE", "1"))


def existing_codes(conn) -> set:
    """Codes we already have - from eval_gold OR already ingested as atar_ edges."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT expected_code AS c FROM kg.eval_gold
            UNION
            SELECT DISTINCT kec.commodity_code AS c
            FROM kg.kg_edge_commodities kec JOIN kg.kg_edges e ON e.id = kec.edge_id
            WHERE e.id LIKE 'atar_%'
        """)
        return {r["c"] for r in cur.fetchall()}


def parse_listing(text: str) -> list:
    """[(ref, code10), ...] from a listing page. Each ruling's code precedes its
    /ruling/<ref> link, so for each link take the last 10-digit code before it."""
    pairs = []
    parts = text.split("/ruling/")
    for k in range(1, len(parts)):
        m = re.match(r"(\d+)", parts[k])
        if not m:
            continue
        ref = m.group(1)
        codes = re.findall(r"\b(\d{10})\b", parts[k - 1])
        code = _flat(codes[-1]) if codes else None
        pairs.append((ref, code))
    return pairs


def main():
    conn = psycopg.connect(DSN, row_factory=dict_row)
    have = existing_codes(conn)
    print(f"existing distinct codes: {len(have)}; target NEW: {TARGET_NEW_CODES}")

    keepers = {}   # code -> ref (first-seen new code)
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for page in range(START_PAGE, START_PAGE + MAX_PAGES):
            if len(keepers) >= TARGET_NEW_CODES:
                break
            try:
                r = client.get(f"{ATAR_BASE}/search?page={page}")
            except Exception as e:
                print(f"  [list p{page}] {e!r}")
                continue
            pairs = parse_listing(r.text)
            if not pairs:
                print(f"page {page}: no entries - end of listing")
                break
            for ref, code in pairs:
                if code and len(code) == 10 and code not in have and code not in keepers:
                    keepers[code] = ref
                    if len(keepers) >= TARGET_NEW_CODES:
                        break
            if page % 10 == 0 or len(keepers) >= TARGET_NEW_CODES:
                print(f"page {page}: NEW distinct codes collected = {len(keepers)}")
            time.sleep(0.2)

    print(f"collected {len(keepers)} new codes; fetching rulings + LLM-extract + upsert...")
    n = 0
    for code, ref in keepers.items():
        ruling = fetch_ruling(ref)
        if not ruling:
            print(f"  [skip {code}] fetch failed (ref {ref})")
            continue
        try:
            facts = llm_extract_facts(ruling)
            upsert_atar(conn, ruling, facts)
            n += 1
            if n % 10 == 0:
                print(f"  ingested {n}/{len(keepers)}")
        except Exception as e:
            print(f"  [ingest {code}] {e!r}")
    print(f"DONE: ingested {n} new ATAR codes (edges + facets). Run persona generators next.")


if __name__ == "__main__":
    main()
