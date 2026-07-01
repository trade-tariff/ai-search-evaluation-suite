"""One-shot seeder for kg.facet_definitions, kg.commodity_facets, kg.kg_edges.

Sources (authoritative only):
  1. Hand-authored slice (commodities.json, facets.json, kg_edges.json) - 15 codes
  2. Local uk.chapter_notes + uk.section_notes -> KG edges
  3. ATAR drafts from ai-fan-out/data/atar_drafts.json -> facets + edges
  4. LLM extraction from goods_nomenclature_descriptions for active 10-digit codes
     in Ch 64, 22, 73 (caps at SEED_LIMIT_PER_CHAPTER).

Run: ENABLE_LLM=1 python seed_facets_kg.py
     (LLM extraction is opt-in; without it only sources 1-3 are seeded.)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

# Load env from project root for OPENAI_API_KEY
from dotenv import load_dotenv
for p in (
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path(__file__).parent.parent/'.env',
):
    if p is not None and p.exists(): load_dotenv(p)

import psycopg
from psycopg.rows import dict_row
try:
    from .evidence_labels import edge_labels, facet_labels
except ImportError:
    from evidence_labels import edge_labels, facet_labels

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
DATA_DIR = Path(__file__).parent / "data"
EXPORT_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
ATAR_PATH = Path(os.environ.get("ATAR_DRAFTS_PATH", EXPORT_DATA_DIR / "atar_drafts.json"))

SEED_CHAPTERS = ["64", "22", "73"]
SEED_LIMIT_PER_CHAPTER = int(os.environ.get("SEED_LIMIT_PER_CHAPTER", "40"))
LLM_MODEL = os.environ.get("SEED_LLM_MODEL", "gpt-5.5")


# --- DB helpers --------------------------------------------------------

def conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def upsert_facet_definition(cur, key, label, short_label, value_set, applies_to_chapters, rank):
    cur.execute(
        """
        INSERT INTO kg.facet_definitions (key, label, short_label, value_set, applies_to_chapters, rank)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (key) DO UPDATE SET
          label=EXCLUDED.label,
          short_label=EXCLUDED.short_label,
          value_set=EXCLUDED.value_set,
          applies_to_chapters=EXCLUDED.applies_to_chapters,
          rank=EXCLUDED.rank
        """,
        (key, label, short_label, json.dumps(value_set), applies_to_chapters, rank),
    )


def insert_commodity_facet(
    cur,
    code,
    key,
    value,
    source,
    confidence=1.0,
    evidence=None,
    authority_tier=6,
    provenance=None,
    use_scopes=None,
    evidence_roles=None,
):
    scopes = use_scopes or _default_facet_use_scopes(source, key)
    roles = evidence_roles or _default_facet_evidence_roles(source, key)
    cur.execute(
        """
        INSERT INTO kg.commodity_facets
          (commodity_code, facet_key, facet_value, source, confidence, evidence, authority_tier, use_scopes, evidence_roles, provenance)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s::jsonb)
        ON CONFLICT (commodity_code, facet_key, facet_value, source) DO NOTHING
        """,
        (_flat(code), key, value, source, confidence, evidence, authority_tier, scopes, roles, json.dumps(provenance or {})),
    )


def upsert_kg_edge(
    cur,
    id_,
    type_,
    scope,
    title,
    body,
    source,
    commodity_codes=None,
    authority_tier=3,
    provenance=None,
    use_scopes=None,
    evidence_roles=None,
):
    scopes = use_scopes or _default_edge_use_scopes(type_, authority_tier)
    roles = evidence_roles or _default_edge_evidence_roles(type_, authority_tier, source=source, edge_id=id_, scope=scope)
    cur.execute(
        """
        INSERT INTO kg.kg_edges (id, type, scope, title, body, source, authority_tier, use_scopes, evidence_roles, provenance)
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
        (id_, type_, scope, title, body, source, authority_tier, scopes, roles, json.dumps(provenance or {})),
    )
    if commodity_codes:
        for code in commodity_codes:
            cur.execute(
                """
                INSERT INTO kg.kg_edge_commodities (edge_id, commodity_code)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                (id_, _flat(code)),
            )


def _flat(code: str) -> str:
    digits = re.sub(r"\D", "", code or "")
    return digits.ljust(10, "0")[:10] if digits else code


def _default_facet_use_scopes(source: str, key: str) -> list[str]:
    return facet_labels(source, key)[0]


def _default_facet_evidence_roles(source: str, key: str) -> list[str]:
    return facet_labels(source, key)[1]


def _default_edge_use_scopes(type_: str, authority_tier: int) -> list[str]:
    return edge_labels(type_, authority_tier)[0]


def _default_edge_evidence_roles(type_: str, authority_tier: int, **kwargs) -> list[str]:
    return edge_labels(type_, authority_tier, **kwargs)[1]


# --- 1. Migrate hand-authored slice -------------------------------------

def seed_handauthored():
    print("=" * 60)
    print("1. Hand-authored slice (commodities.json + facets.json + kg_edges.json)")
    facets_doc = json.loads((DATA_DIR / "facets.json").read_text())["facets"]
    commodities = json.loads((DATA_DIR / "commodities.json").read_text())["commodities"]
    kg_edges = json.loads((DATA_DIR / "kg_edges.json").read_text())["edges"]

    with conn() as c, c.cursor() as cur:
        # Facet definitions
        for key, fdef in facets_doc.items():
            value_set = [
                {"value": v, "label": fdef.get("value_labels", {}).get(v, v)}
                for v in fdef.get("values", [])
            ]
            upsert_facet_definition(
                cur, key,
                fdef.get("label", key),
                fdef.get("short_label"),
                value_set,
                fdef.get("applies_to_chapters", []),
                fdef.get("rank", 99),
            )

        # Per-commodity facets - auto-create any facet_definition missing from facets.json
        n_facets = 0
        for com in commodities:
            for facet_key, value in (com.get("facets") or {}).items():
                if value is None: continue
                cur.execute(
                    "INSERT INTO kg.facet_definitions (key, label) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (facet_key, facet_key.replace("_", " ").capitalize()),
                )
                insert_commodity_facet(
                    cur, com["code"], facet_key, str(value),
                    source="hand",
                    evidence="Hand-authored fact sheet",
                )
                n_facets += 1

        # KG edges + their applicable commodities
        n_edges = 0
        for edge in kg_edges:
            scope = edge.get("scope", "global")
            # Link to commodities that referenced this edge in commodities.json
            cc_codes = [com["code"] for com in commodities if edge["id"] in (com.get("kg_edges") or [])]
            upsert_kg_edge(
                cur, edge["id"], edge.get("type", "rule"),
                scope, edge["title"], edge["body"], edge["source"],
                commodity_codes=cc_codes,
            )
            n_edges += 1

        c.commit()
        print(f"  facet definitions: {len(facets_doc)}")
        print(f"  per-commodity facets: {n_facets}")
        print(f"  KG edges: {n_edges}")


# --- 2. Chapter + section notes from local DB ---------------------------

def seed_chapter_section_notes():
    print("=" * 60)
    print("2. Chapter notes + section notes from local DB -> KG edges")
    with conn() as c, c.cursor() as cur:
        # Chapter notes
        cur.execute("SELECT chapter_id, section_id, content FROM uk.chapter_notes")
        chapter_rows = cur.fetchall()
        n = 0
        for r in chapter_rows:
            ch = r["chapter_id"]
            content = (r["content"] or "").strip()
            if not content: continue
            # Each chapter_notes row is a single concatenated note text - keep as one edge
            edge_id = f"ch{ch}_notes"
            upsert_kg_edge(
                cur, edge_id, "definition",
                f"chapter:{ch}",
                f"Chapter {ch} legal notes",
                content[:4000],  # cap for prompt budget
                source=f"UK Tariff Chapter {ch} Notes",
            )
            n += 1
        print(f"  chapter notes seeded: {n}")

        # Section notes
        cur.execute("SELECT section_id, content FROM uk.section_notes")
        section_rows = cur.fetchall()
        n2 = 0
        for r in section_rows:
            sec = r["section_id"]
            content = (r["content"] or "").strip()
            if not content: continue
            edge_id = f"sec{sec}_notes"
            upsert_kg_edge(
                cur, edge_id, "definition",
                f"section:{sec}",
                f"Section {sec} legal notes",
                content[:4000],
                source=f"UK Tariff Section {sec} Notes",
            )
            n2 += 1
        print(f"  section notes seeded: {n2}")

        c.commit()


# --- 3. ATAR rulings ----------------------------------------------------

def seed_atars():
    print("=" * 60)
    print("3. ATAR rulings -> commodity_facets + KG edges (rationale)")
    if not ATAR_PATH.exists():
        print(f"  no ATAR file at {ATAR_PATH}; skipping")
        return
    doc = json.loads(ATAR_PATH.read_text())
    drafts = doc.get("drafts", [])
    if not drafts:
        print("  empty ATAR file; skipping")
        return

    with conn() as c, c.cursor() as cur:
        n_edges = 0
        n_facets = 0
        for d in drafts:
            ruling = d.get("ruling") or {}
            cc = ruling.get("commodity_code")
            if not cc: continue
            ref = ruling.get("ref") or "?"
            edge_id = f"atar_{ref}"
            title = f"ATAR {ref} ({cc})"
            body_parts = []
            if ruling.get("description"):
                body_parts.append(f"Product: {ruling['description']}")
            if ruling.get("justification"):
                body_parts.append(f"HMRC classification rationale: {ruling['justification']}")
            body = "\n\n".join(body_parts)[:4000]
            upsert_kg_edge(
                cur, edge_id, "rationale",
                f"commodity:{_flat(cc)}",
                title, body,
                source=f"HMRC Advance Tariff Ruling {ref}",
                commodity_codes=[cc],
                authority_tier=2,
                provenance={
                    "source_type": "atar",
                    "source_id": ref,
                    "scope_ref": f"commodity:{_flat(cc)}",
                },
            )
            n_edges += 1

            # Pull the gold_facts (LLM-extracted by fan-out) if present.
            gf = d.get("gold_facts") or []
            for f in gf:
                slot = f.get("slot")
                ans = f.get("answer")
                if not slot or not ans: continue
                # Auto-create a placeholder facet_definition if missing
                cur.execute(
                    "INSERT INTO kg.facet_definitions (key, label) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                    (slot, slot.replace("_", " ").capitalize()),
                )
                insert_commodity_facet(
                    cur, cc, slot, str(ans),
                    source=f"atar:{ref}",
                    evidence=(f.get("source_question") or "")[:500],
                    authority_tier=5,
                    provenance={
                        "source_type": "atar",
                        "source_id": ref,
                        "extracted_by": d.get("generator") or "ai-fan-out",
                    },
                )
                n_facets += 1
        c.commit()
        print(f"  ATAR rationale edges: {n_edges}")
        print(f"  ATAR-extracted facets: {n_facets}")


# --- 4. LLM facet extraction from tariff descriptions -------------------

EXTRACTION_PROMPT = """You are extracting structured facts about a goods commodity for an AI-assisted classification system.

Given the commodity code and its tariff descriptions, output a JSON object of structured facts as {slot: value} pairs.

Use snake_case slot names (e.g. material_upper, beverage_type, mounting). Pick values that would be useful as multiple-choice answer options when narrowing similar codes. Use short answers (1-3 words).

Only output facts that are clearly stated or strongly implied by the descriptions. Don't invent.

Examples:
  Input: 6402200000 - Footwear with upper straps or thongs assembled to the sole by means of plugs
  Output: {"material_upper": "rubber_or_plastic", "material_sole": "rubber_or_plastic", "closure": "strap_thong", "construction": "plug_assembled"}

  Input: 2204101400 - Sparkling wine of fresh grapes, of an actual alcoholic strength by volume of not less than 8.5%, in containers of a holding capacity not exceeding 2 litres
  Output: {"beverage_type": "wine", "still_or_sparkling": "sparkling", "container_size": "le_2L", "alcohol_band": "8.5_to_22"}

Respond with the JSON object only, no preamble, no markdown."""


def llm_extract_facets(code: str, descriptions: list[str]) -> dict:
    """Returns {slot: value} dict, or {} on failure."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key: return {}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        user = f"Commodity: {code}\nDescriptions:\n" + "\n".join(f"- {d}" for d in descriptions if d)
        kwargs = {
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
            kwargs["max_tokens"] = 300
            kwargs["temperature"] = 0.0
        r = client.chat.completions.create(**kwargs)
        text = (r.choices[0].message.content or "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"  ! LLM extraction failed for {code}: {type(e).__name__} {e}")
        return {}


def seed_llm_facets():
    if not os.environ.get("ENABLE_LLM"):
        print("=" * 60)
        print("4. LLM extraction skipped (ENABLE_LLM not set)")
        return

    print("=" * 60)
    print(f"4. LLM facet extraction from descriptions/self_texts, ~{SEED_LIMIT_PER_CHAPTER}/chapter")

    with conn() as c, c.cursor() as cur:
        # For each target chapter, pull active 10-digit codes that have a description.
        for chapter in SEED_CHAPTERS:
            cur.execute(
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
                  AND LEFT(gn.goods_nomenclature_item_id, 2) = %s
                  AND gnd.description IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM kg.commodity_facets cf
                    WHERE cf.commodity_code = gn.goods_nomenclature_item_id
                  )
                ORDER BY gn.goods_nomenclature_item_id
                LIMIT %s
                """,
                (chapter, SEED_LIMIT_PER_CHAPTER),
            )
            rows = cur.fetchall()
            print(f"  Ch {chapter}: extracting facets for {len(rows)} codes")
            for i, r in enumerate(rows, 1):
                code = r["code"]
                descs = [r.get("description") or "", r.get("self_text") or ""]
                facets = llm_extract_facets(code, [d for d in descs if d])
                if not facets:
                    continue
                # Insert each fact; auto-create facet_definitions if missing.
                with conn() as c2, c2.cursor() as cur2:
                    for slot, value in facets.items():
                        if not slot or value in (None, "", "unknown", "Unknown"):
                            continue
                        cur2.execute(
                            "INSERT INTO kg.facet_definitions (key, label, applies_to_chapters) VALUES (%s, %s, ARRAY[%s]) ON CONFLICT (key) DO UPDATE SET applies_to_chapters = (SELECT array_agg(DISTINCT x) FROM unnest(kg.facet_definitions.applies_to_chapters || ARRAY[%s]) x)",
                            (slot, slot.replace("_", " ").capitalize(), chapter, chapter),
                        )
                        insert_commodity_facet(
                            cur2, code, slot, str(value),
                            source="description_llm",
                            confidence=0.85,
                            evidence=(r.get("description") or "")[:500],
                        )
                    c2.commit()
                if i % 10 == 0:
                    print(f"    ... {i}/{len(rows)}")


def main():
    seed_handauthored()
    seed_chapter_section_notes()
    seed_atars()
    seed_llm_facets()
    # Summary
    with conn() as c, c.cursor() as cur:
        for t in ("facet_definitions", "commodity_facets", "kg_edges", "kg_edge_commodities"):
            cur.execute(f"SELECT count(*) AS n FROM kg.{t}")
            print(f"  kg.{t}: {cur.fetchone()['n']}")


if __name__ == "__main__":
    main()
