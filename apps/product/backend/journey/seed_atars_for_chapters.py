"""Scrape ATAR rulings for target chapters and ingest into kg.

GOV.UK ATAR search supports `?keyword=...` filtering, so we hit it with a
basket of chapter-relevant keywords, dedupe by ref, filter to the target
chapters, fetch the full ruling text, LLM-extract structured facts via
gpt-5.5, and write to kg.commodity_facets + kg.kg_edges.

Idempotent: ON CONFLICT skips already-ingested rulings.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
for p in (
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path(__file__).parent.parent / ".env",
):
    if p is not None and p.exists():
        load_dotenv(p)

import httpx
import psycopg
from bs4 import BeautifulSoup
from psycopg.rows import dict_row
try:
    from .evidence_labels import edge_labels, facet_labels
except ImportError:
    from evidence_labels import edge_labels, facet_labels

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
ATAR_BASE = "https://www.tax.service.gov.uk/search-for-advance-tariff-rulings"
USER_AGENT = "hmrc-poc-research-scraper/1.0"
LLM_MODEL = os.environ.get("ATAR_LLM_MODEL", "gpt-5.5")

# Chapter -> keywords. Code-prefix searches are much more accurate than
# generic words ("shoe" returns Ch 62 clothing too). Mix codes + specific words.
# Corpus-expansion map (2026-06-02): 50/50 stress (residual/ambiguity-heavy) +
# representative (breadth). Existing 22/64/73 ATARs are already in the KG, so this
# set targets NEW chapters to push the corpus from 47 -> 100+ CCs. Code-prefix
# searches are the reliable signal; a few words help recall.
CHAPTER_KEYWORDS = {
    # --- stress: residual "Other"/NES + material-vs-form-vs-function ambiguity ---
    "39": ["3915", "3916", "3917", "3918", "3919", "3920", "3921", "3923", "3924",
           "3925", "3926", "plastic article", "polythene", "pvc"],
    "40": ["4008", "4009", "4010", "4011", "4012", "4013", "4014", "4015", "4016",
           "4017", "rubber article", "rubber tube", "rubber seal"],
    "61": ["6101", "6103", "6104", "6105", "6106", "6107", "6108", "6109", "6110",
           "6111", "6112", "6113", "6114", "6115", "6116", "6117", "knitted garment"],
    "62": ["6201", "6202", "6203", "6204", "6205", "6206", "6210", "6211", "6212",
           "6213", "6214", "6216", "6217", "woven garment", "jacket"],
    "63": ["6301", "6302", "6303", "6304", "6305", "6306", "6307", "6308", "6310",
           "blanket", "tarpaulin", "textile article"],
    "84": ["8413", "8414", "8419", "8421", "8424", "8467", "8471", "8479", "8481",
           "machine part", "pump", "valve"],
    "85": ["8501", "8504", "8507", "8513", "8516", "8517", "8518", "8528", "8536",
           "8543", "8544", "electrical apparatus", "connector"],
    "95": ["9503", "9504", "9505", "9506", "9507", "9508", "toy", "game",
           "sports equipment"],
    # --- representative: breadth across the tariff ---
    "33": ["3301", "3302", "3303", "3304", "3305", "3306", "3307", "perfume",
           "cosmetic", "shampoo"],
    "44": ["4407", "4409", "4411", "4412", "4418", "4419", "4420", "4421",
           "wood article", "plywood"],
    "48": ["4802", "4810", "4811", "4818", "4819", "4820", "4821", "4823",
           "paper article", "cardboard"],
    "70": ["7007", "7009", "7010", "7013", "7019", "7020", "glass article",
           "glassware"],
    "87": ["8708", "8712", "8714", "8716", "vehicle part", "bicycle", "trailer"],
    "94": ["9401", "9403", "9404", "9405", "furniture", "lamp", "mattress"],
    # --- original pre-expansion chapters (preserved; re-scrape is a no-op via dedup) ---
    "22": ["2201", "2202", "2203", "2204", "2205", "2206", "2207", "2208", "2209",
           "wine", "beer", "spirits", "whisky", "vodka", "gin", "champagne",
           "cider", "rum", "tequila", "liqueur"],
    "64": ["6401", "6402", "6403", "6404", "6405", "6406",
           "footwear", "shoes", "sneakers", "boots", "sandals", "slippers",
           "trainers", "flip flops"],
    "73": ["7301", "7302", "7303", "7304", "7305", "7306", "7307", "7308", "7309",
           "7310", "7311", "7312", "7313", "7314", "7315", "7316", "7317", "7318",
           "7319", "7320", "7321", "7322", "7323", "7324", "7325", "7326",
           "stainless steel", "iron article", "steel article"],
    # --- pass 3 breadth: more chapters for distinct-CC coverage toward 100+ ---
    "16": ["1601", "1602", "1604", "1605", "prepared meat", "sausage", "fish preparation"],
    "17": ["1701", "1702", "1704", "1806", "sugar", "confectionery", "chocolate"],
    "19": ["1901", "1902", "1904", "1905", "pasta", "biscuit", "bread", "cereal"],
    "20": ["2005", "2007", "2008", "2009", "preserved vegetable", "fruit juice", "jam"],
    "21": ["2103", "2104", "2106", "sauce", "food preparation", "supplement", "extract"],
    "30": ["3002", "3003", "3004", "3005", "3006", "medicament", "pharmaceutical", "dressing"],
    "32": ["3204", "3206", "3208", "3209", "3215", "paint", "ink", "dye"],
    "34": ["3401", "3402", "3403", "3406", "soap", "detergent", "candle", "polish"],
    "42": ["4202", "4203", "handbag", "leather article", "wallet", "case"],
    "48": ["4817", "4818", "4819", "4820", "4823", "envelope", "label", "filter paper"],
    "49": ["4901", "4909", "4910", "4911", "printed", "book", "calendar", "sticker"],
    "65": ["6505", "6506", "6507", "hat", "headgear", "cap", "helmet"],
    "68": ["6802", "6810", "6815", "stone article", "concrete", "abrasive"],
    "69": ["6907", "6910", "6911", "6912", "6913", "ceramic", "tile", "tableware"],
    "71": ["7113", "7116", "7117", "jewellery", "imitation jewellery", "bracelet"],
    "82": ["8205", "8211", "8213", "8214", "8215", "hand tool", "knife", "cutlery"],
    "83": ["8301", "8302", "8304", "8308", "8310", "lock", "fitting", "sign plate"],
    "90": ["9004", "9013", "9018", "9025", "9027", "9031", "optical", "medical instrument"],
    "91": ["9101", "9102", "9105", "9111", "watch", "clock"],
    "96": ["9603", "9606", "9608", "9613", "9617", "brush", "pen", "lighter", "vacuum flask"],
}

TIMEOUT_S = 30
MAX_RULINGS_PER_CHAPTER = int(os.environ.get("MAX_RULINGS_PER_CHAPTER", "60"))
SEARCH_PAGES_PER_KEYWORD = int(os.environ.get("SEARCH_PAGES_PER_KEYWORD", "3"))


# --- HTML scraping ----------------------------------------------------

def search_for_refs(keyword: str, max_pages: int = SEARCH_PAGES_PER_KEYWORD) -> list[str]:
    """Return ATAR ruling refs matching the keyword search."""
    refs: list[str] = []
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=TIMEOUT_S, headers=headers) as client:
        for page in range(1, max_pages + 1):
            url = f"{ATAR_BASE}/search?keyword={keyword}&page={page}"
            try:
                r = client.get(url)
                r.raise_for_status()
            except Exception as e:
                print(f"  [search '{keyword}' p{page}] {e}")
                continue
            page_refs = re.findall(r"/ruling/(\d+)", r.text)
            if not page_refs:
                break
            refs.extend(page_refs)
    return list(dict.fromkeys(refs))  # dedupe, preserve order


def fetch_ruling(ref: str) -> dict | None:
    """Fetch a single ruling, parse out description + justification + commodity code."""
    headers = {"User-Agent": USER_AGENT}
    try:
        with httpx.Client(timeout=TIMEOUT_S, headers=headers) as client:
            r = client.get(f"{ATAR_BASE}/ruling/{ref}")
            r.raise_for_status()
    except Exception as e:
        print(f"  [fetch {ref}] {type(e).__name__}: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    fields: dict[str, str] = {}
    # The page has a definition list with the ruling details.
    for dt in soup.find_all("dt"):
        key = dt.get_text(" ", strip=True).rstrip(":").strip().lower()
        dd = dt.find_next_sibling("dd")
        if dd:
            fields[key] = dd.get_text(" ", strip=True)
    code = fields.get("commodity code", "")
    code = re.sub(r"\D", "", code)
    return {
        "ref": ref,
        "commodity_code": code,
        "description": fields.get("description of the goods", "") or fields.get("description", ""),
        "justification": fields.get("justification", "") or fields.get("rationale", ""),
        "keywords": fields.get("keywords", ""),
        "url": f"{ATAR_BASE}/ruling/{ref}",
    }


# --- LLM fact extraction ----------------------------------------------

EXTRACTION_PROMPT = """You are extracting structured commodity facts from an HMRC Advance Tariff Ruling.

The ruling gives a specific real product, its commodity code, and HMRC's reasoning. Your job is to read the description + justification and output structured facts that capture what was discriminating about this product for classification.

Return JSON of the form `{"facts": [{"slot": "...", "value": "..."}, ...]}`.

Slot guidelines:
- Use snake_case slot names (e.g. material_upper, beverage_type, bottle_size, abv_pct, intended_use).
- Pick attributes that would be useful as multiple-choice answers when narrowing similar commodity codes.
- Short values (1-4 words). E.g. "rubber_or_plastic", "above_22", "single_malt".
- Only output facts clearly stated or strongly implied. Don't invent.
- Aim for 5-12 facts per ruling.

Example output:
{"facts":[
  {"slot":"material_upper","value":"rubber_or_plastic"},
  {"slot":"material_sole","value":"rubber_or_plastic"},
  {"slot":"closure","value":"strap_thong"},
  {"slot":"construction","value":"plug_assembled"},
  {"slot":"intended_use","value":"casual"}
]}"""


def llm_extract_facts(ruling: dict) -> list[dict]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return []
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        user = (
            f"Commodity code: {ruling['commodity_code']}\n\n"
            f"Description:\n{ruling.get('description','')[:3000]}\n\n"
            f"Justification:\n{ruling.get('justification','')[:3000]}"
        )
        kwargs = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        if LLM_MODEL.startswith("gpt-5"):
            kwargs["max_completion_tokens"] = 1200
        else:
            kwargs["max_tokens"] = 600
            kwargs["temperature"] = 0.0
        r = client.chat.completions.create(**kwargs)
        text = (r.choices[0].message.content or "").strip()
        d = json.loads(text)
        facts = d.get("facts", []) if isinstance(d, dict) else []
        return [
            {"slot": f.get("slot"), "value": f.get("value")}
            for f in facts
            if isinstance(f, dict) and f.get("slot") and f.get("value") is not None
        ]
    except Exception as e:
        print(f"  [llm extract] {type(e).__name__}: {e}")
        return []


# --- DB inserts -------------------------------------------------------

def _flat(code: str) -> str:
    digits = re.sub(r"\D", "", code or "")
    return digits.ljust(10, "0")[:10] if digits else code


def upsert_atar(conn, ruling: dict, facts: list[dict]) -> tuple[int, int]:
    """Insert KG edge + facts. Returns (facts_inserted, was_new_edge)."""
    with conn.cursor() as cur:
        code = _flat(ruling["commodity_code"])
        if not code:
            return 0, 0
        ref = ruling["ref"]
        edge_id = f"atar_{ref}"
        body_parts = []
        if ruling.get("description"):
            body_parts.append(f"Product: {ruling['description']}")
        if ruling.get("justification"):
            body_parts.append(f"HMRC justification: {ruling['justification']}")
        body = ("\n\n".join(body_parts))[:4000]
        provenance = {
            "source_type": "atar",
            "source_id": ref,
            "url": ruling.get("url"),
            "keywords": ruling.get("keywords"),
            "scope_ref": f"commodity:{code}",
        }
        edge_use_scopes, edge_roles = edge_labels(
            "rationale", 2, source=f"HMRC Advance Tariff Ruling {ref}", edge_id=edge_id, scope=f"commodity:{code}"
        )

        # KG edge for the ruling itself
        cur.execute(
            """
            INSERT INTO kg.kg_edges
              (id, type, scope, title, body, source, authority_tier, use_scopes, evidence_roles, provenance)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
              type=EXCLUDED.type, scope=EXCLUDED.scope, title=EXCLUDED.title,
              body=EXCLUDED.body, source=EXCLUDED.source,
              authority_tier=EXCLUDED.authority_tier,
              use_scopes=EXCLUDED.use_scopes,
              evidence_roles=EXCLUDED.evidence_roles,
              provenance=EXCLUDED.provenance,
              updated_at=now()
            """,
            (
                edge_id,
                "rationale",
                f"commodity:{code}",
                f"ATAR {ref} ({code})",
                body,
                f"HMRC Advance Tariff Ruling {ref} - {ruling['url']}",
                2,
                edge_use_scopes,
                edge_roles,
                json.dumps(provenance),
            ),
        )
        cur.execute(
            "INSERT INTO kg.kg_edge_commodities (edge_id, commodity_code) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (edge_id, code),
        )

        # Facts
        n = 0
        for f in facts:
            slot = f["slot"]
            value = str(f["value"])
            facet_use_scopes, facet_roles = facet_labels(f"atar:{ref}", slot, value)
            # Auto-create facet definition if missing
            cur.execute(
                "INSERT INTO kg.facet_definitions (key, label) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (slot, slot.replace("_", " ").capitalize()),
            )
            cur.execute(
                """
                INSERT INTO kg.commodity_facets
                  (commodity_code, facet_key, facet_value, source, confidence, evidence, authority_tier, use_scopes, evidence_roles, provenance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s::jsonb)
                ON CONFLICT (commodity_code, facet_key, facet_value, source) DO NOTHING
                """,
                (
                    code,
                    slot,
                    value,
                    f"atar:{ref}",
                    0.95,
                    (ruling.get("description") or "")[:500],
                    5,
                    facet_use_scopes,
                    facet_roles,
                    json.dumps({
                        "source_type": "atar",
                        "source_id": ref,
                        "url": ruling.get("url"),
                        "extracted_by": LLM_MODEL,
                    }),
                ),
            )
            n += 1
        conn.commit()
        return n, 1


# --- Driver -----------------------------------------------------------

def main():
    conn = psycopg.connect(DSN, row_factory=dict_row)

    all_refs_by_chapter: dict[str, set[str]] = {ch: set() for ch in CHAPTER_KEYWORDS}
    chapter_for_ref: dict[str, str] = {}

    print("Phase 1: collecting candidate refs by keyword search...")
    for chapter, keywords in CHAPTER_KEYWORDS.items():
        print(f"  Ch {chapter}: searching {len(keywords)} keywords")
        for kw in keywords:
            refs = search_for_refs(kw, max_pages=2)
            for ref in refs:
                all_refs_by_chapter[chapter].add(ref)
            time.sleep(0.5)  # be polite

    # Dedup refs across chapters - keep first chapter assignment
    refs_to_fetch: list[tuple[str, str]] = []  # (ref, target_chapter)
    seen: set[str] = set()
    for chapter in CHAPTER_KEYWORDS:
        for ref in all_refs_by_chapter[chapter]:
            if ref in seen:
                continue
            seen.add(ref)
            refs_to_fetch.append((ref, chapter))

    print(f"\n  total unique refs across keyword searches: {len(refs_to_fetch)}")

    # Skip refs we already have
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM kg.kg_edges WHERE id LIKE 'atar_%'")
        existing = {row["id"].removeprefix("atar_") for row in cur.fetchall()}
    refs_to_fetch = [(ref, ch) for ref, ch in refs_to_fetch if ref not in existing]
    print(f"  refs not already in KG: {len(refs_to_fetch)}")

    # Phase 2: fetch + extract + insert.
    # We accept ATARs from ANY chapter the keyword search returns - "shoes" → Ch 62
    # (shoe protectors), "wine" → Ch 21 (wine sauces), etc. all become legitimate KG
    # context. The cap is per-chapter so we still spread coverage, but we don't drop
    # adjacent-chapter rulings as "wrong".
    print(f"\nPhase 2: fetching + extracting (~{LLM_MODEL})...")
    inserted = 0
    facts_total = 0
    per_chapter_count: dict[str, int] = {}
    for i, (ref, target_chapter) in enumerate(refs_to_fetch, 1):
        ruling = fetch_ruling(ref)
        if not ruling or not ruling["commodity_code"]:
            continue
        actual_chapter = ruling["commodity_code"][:2]
        n_in_chapter = per_chapter_count.get(actual_chapter, 0)
        if n_in_chapter >= MAX_RULINGS_PER_CHAPTER:
            continue
        facts = llm_extract_facts(ruling)
        if not facts:
            print(f"    [{i}/{len(refs_to_fetch)}] {ref}: no facts extracted")
            continue
        n, _ = upsert_atar(conn, ruling, facts)
        inserted += 1
        facts_total += n
        per_chapter_count[actual_chapter] = n_in_chapter + 1
        adjacent = "" if actual_chapter == target_chapter else f" (adjacent to target Ch {target_chapter})"
        print(f"    [{i}/{len(refs_to_fetch)}] {ref} -> Ch {actual_chapter} ({ruling['commodity_code']}): {n} facts{adjacent}")
        time.sleep(0.3)

    print(f"\nDone. Ingested {inserted} ATARs, {facts_total} facts.")
    print(f"  per chapter: {dict(sorted(per_chapter_count.items()))}")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM kg.commodity_facets WHERE source LIKE 'atar:%'")
        print(f"  total ATAR-sourced facets in KG: {cur.fetchone()['n']}")
        cur.execute("SELECT count(*) AS n FROM kg.kg_edges WHERE id LIKE 'atar_%'")
        print(f"  total ATAR edges in KG: {cur.fetchone()['n']}")


if __name__ == "__main__":
    main()
