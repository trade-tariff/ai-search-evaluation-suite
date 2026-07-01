"""LLM-decompose chapter notes + section notes into individual structured rules.

For each row in uk.chapter_notes / uk.section_notes:
  1. Pull the full note text
  2. Ask LLM to break it into individual notes (Note 1, Note 2, sub-notes 1(a) etc.)
  3. For each one, get: id-suffix, type (exclusion|definition|discriminator|order|other),
     title, body, referenced_codes (chapter/heading/CC IDs mentioned in the text)
  4. Replace any existing per-chapter blob edge (chXX_notes) with the decomposed rules
  5. Insert kg_edge_commodities rows for any 4/6/10-digit codes explicitly referenced

Idempotent: re-running deletes prior decomposed edges for the chapter before re-inserting.
Cost: ~$5 total at gpt-5.5 for all 112 chapter+section notes; can scope via env.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
for p in (
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path(__file__).parent.parent/'.env',
):
    if p is not None and p.exists(): load_dotenv(p)

import psycopg
from psycopg.rows import dict_row
try:
    from .evidence_labels import edge_labels
except ImportError:
    from evidence_labels import edge_labels

DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
LLM_MODEL = os.environ.get("DECOMPOSE_LLM_MODEL", "gpt-5.5")

# Limit to a subset of chapters via env. Empty = all.
CHAPTERS_ENV = os.environ.get("DECOMPOSE_CHAPTERS", "").strip()
TARGET_CHAPTERS: list[str] | None = [c.strip() for c in CHAPTERS_ENV.split(",") if c.strip()] or None

# Limit to a subset of sections via env. Empty = all.
SECTIONS_ENV = os.environ.get("DECOMPOSE_SECTIONS", "").strip()
TARGET_SECTIONS: list[str] | None = [s.strip() for s in SECTIONS_ENV.split(",") if s.strip()] or None


DECOMPOSE_PROMPT = """You are parsing UK Tariff legal notes (chapter notes, section notes) into structured rules so they can be used to ground LLM classification reasoning.

Read the input note text and return a JSON object of the form `{"rules": [...]}`. The `rules` array contains one entry per numbered note or sub-note (e.g. "1(a)", "1(b)", "2", "3", "4(a)"). Do NOT split into smaller pieces than the actual notes - keep each numbered note/sub-note intact.

For each rule extract:

  - note_ref: the note label as it appears, e.g. "1", "1(a)", "1(b)", "2", "3(a)", "Subheading Note 1"
  - type: one of:
      "exclusion"           - "This chapter does not cover X"
      "inclusion"           - "This chapter includes X"
      "definition"          - "The term X means / refers to Y"
      "classification_order" - "X shall be classified first by ..."
      "discriminator"       - "X applies only when ..."
      "other"               - anything else
  - title: ONE clear sentence summarising the rule (max ~15 words). Examples:
      - "Chapter 64 excludes worn footwear of heading 6309"
      - "The term 'rubber' includes textile products with visible rubber coating"
      - "Sports footwear definition for heading 6404"
  - body: the verbatim text of just this note/sub-note (no commentary, no Markdown).
  - references: an array of any explicit codes mentioned in the note. Use the form:
      - 2-digit chapter:    "ch:64"
      - 4-digit heading:    "h:6309"
      - 6-digit subheading: "sub:640690"
      - 10-digit CC:        "cc:6406900000"
    Include any code that the note references as a cross-link target. If the note says "Section XI", emit "sec:XI".

Output ONLY the JSON object {"rules": [...]}. No preamble, no explanation, no markdown fences.

Example output shape:
{"rules": [
  {"note_ref": "1(a)", "type": "exclusion", "title": "Excludes disposable foot coverings",
   "body": "disposable foot or shoe coverings of flimsy material...", "references": []},
  {"note_ref": "1(c)", "type": "exclusion", "title": "Excludes worn footwear",
   "body": "worn footwear of heading 6309;", "references": ["h:6309"]}
]}
"""


def llm_decompose(scope_label: str, note_text: str) -> list[dict]:
    """Call the LLM. Returns a list of structured rules or [] on failure."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("[decompose] OPENAI_API_KEY missing; skipping LLM call")
        return []
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        kwargs = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": DECOMPOSE_PROMPT},
                {"role": "user", "content": f"Scope: {scope_label}\n\nNote text:\n{note_text}"},
            ],
            "response_format": {"type": "json_object"},
        }
        # gpt-5.x uses max_completion_tokens; we need ~4k for output (many sub-notes).
        if LLM_MODEL.startswith("gpt-5"):
            kwargs["max_completion_tokens"] = 6000
        else:
            kwargs["max_tokens"] = 4000
            kwargs["temperature"] = 0.0
        r = client.chat.completions.create(**kwargs)
        text = (r.choices[0].message.content or "").strip()
        # response_format json_object always returns a dict at the top. Find the rules list inside.
        data = json.loads(text)
        candidate_list: list = []
        if isinstance(data, list):
            candidate_list = data
        elif isinstance(data, dict):
            for key in ("rules", "notes", "items", "result", "data"):
                if isinstance(data.get(key), list):
                    candidate_list = data[key]
                    break
            if not candidate_list:
                for v in data.values():
                    if isinstance(v, list):
                        candidate_list = v
                        break
        # Filter to dict items only - LLM sometimes returns strings instead of objects.
        clean = [it for it in candidate_list if isinstance(it, dict)]
        if len(clean) < len(candidate_list):
            print(f"    [decompose] dropped {len(candidate_list)-len(clean)} non-dict items in response")
        return clean
    except Exception as e:
        print(f"[decompose] LLM error for {scope_label}: {type(e).__name__}: {e}")
        return []


def _flat_code(ref: str) -> tuple[str, str] | None:
    """Convert a reference like 'h:6309' or 'cc:6406900000' or 'ch:64' to (flat_code, kind).
    Returns None if we can't normalise it."""
    if ":" not in ref:
        return None
    kind, body = ref.split(":", 1)
    digits = re.sub(r"\D", "", body)
    if kind == "ch" and len(digits) == 2:
        return (digits.ljust(10, "0"), "chapter")  # we link to the canonical 10-digit chapter root
    if kind == "h" and len(digits) == 4:
        return (digits.ljust(10, "0"), "heading")
    if kind == "sub" and len(digits) == 6:
        return (digits.ljust(10, "0"), "subheading")
    if kind == "cc" and len(digits) == 10:
        return (digits, "commodity")
    if kind == "sec":
        return (None, f"section:{body}")  # not a commodity, just a scope reference
    return None


def insert_rules(cur, scope: str, source: str, scope_prefix_id: str, rules: list[dict]):
    """Replace any prior decomposed edges with this scope_prefix_id pattern."""
    # Wipe earlier decomposed edges for this chapter/section (id starts with prefix + '_note_')
    cur.execute(
        "DELETE FROM kg.kg_edges WHERE id LIKE %s",
        (f"{scope_prefix_id}_note_%",),
    )
    n_inserted = 0
    n_linked = 0
    for r in rules:
        note_ref = (r.get("note_ref") or "").strip()
        if not note_ref:
            continue
        # Normalise edge id to a safe slug
        slug = re.sub(r"[^a-z0-9]+", "_", note_ref.lower()).strip("_")
        edge_id = f"{scope_prefix_id}_note_{slug}"
        rule_type = (r.get("type") or "other").strip()
        if rule_type == "order":
            rule_type = "classification_order"
        title = (r.get("title") or f"{scope_prefix_id} note {note_ref}").strip()
        body = (r.get("body") or "").strip()
        if not body:
            continue
        use_scopes, evidence_roles = edge_labels(rule_type, 1, source=source, edge_id=edge_id, scope=scope)
        cur.execute(
            """
            INSERT INTO kg.kg_edges
              (id, type, scope, title, body, source, authority_tier, use_scopes, evidence_roles, provenance)
            VALUES (%s, %s, %s, %s, %s, %s, 1, %s::text[], %s::text[], %s::jsonb)
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
                edge_id, rule_type, scope, f"{title} (Note {note_ref})", body, source,
                use_scopes, evidence_roles,
                json.dumps({
                    "source_type": "chapter_section_note",
                    "scope_ref": scope,
                    "note_ref": note_ref,
                    "references": r.get("references") or [],
                    "extractor": "seed_notes_decomposition",
                    "extractor_model": LLM_MODEL,
                }),
            ),
        )
        n_inserted += 1
        # Cross-link to explicitly referenced codes
        for ref in (r.get("references") or []):
            normalised = _flat_code(ref)
            if normalised is None:
                continue
            code, _ = normalised
            if code:
                cur.execute(
                    """
                    INSERT INTO kg.kg_edge_commodities (edge_id, commodity_code)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING
                    """,
                    (edge_id, code),
                )
                n_linked += 1
    return n_inserted, n_linked


def decompose_chapter_notes():
    print("=" * 60)
    print("Decomposing chapter notes...")
    with psycopg.connect(DSN, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute("SELECT chapter_id, content FROM uk.chapter_notes WHERE content IS NOT NULL ORDER BY chapter_id")
        rows = cur.fetchall()
        if TARGET_CHAPTERS:
            rows = [r for r in rows if r["chapter_id"] in TARGET_CHAPTERS]
        print(f"  {len(rows)} chapters to process")
        total_rules = 0
        total_links = 0
        for i, r in enumerate(rows, 1):
            chapter = r["chapter_id"]
            content = (r["content"] or "").strip()
            if len(content) < 50:
                continue
            scope = f"chapter:{chapter}"
            scope_prefix_id = f"ch{chapter}"
            rules = llm_decompose(scope, content)
            if not rules:
                print(f"    [{i}/{len(rows)}] Ch {chapter}: no rules extracted, skipping")
                continue
            n_ins, n_lnk = insert_rules(cur, scope, f"UK Tariff Chapter {chapter} Notes", scope_prefix_id, rules)
            total_rules += n_ins
            total_links += n_lnk
            print(f"    [{i}/{len(rows)}] Ch {chapter}: {n_ins} rules, {n_lnk} cross-links")
            c.commit()
        print(f"  total rules: {total_rules}, cross-links: {total_links}")


def decompose_section_notes():
    print("=" * 60)
    print("Decomposing section notes...")
    with psycopg.connect(DSN, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute("SELECT section_id, content FROM uk.section_notes WHERE content IS NOT NULL ORDER BY section_id")
        rows = cur.fetchall()
        if TARGET_SECTIONS:
            rows = [r for r in rows if str(r["section_id"]) in TARGET_SECTIONS]
        print(f"  {len(rows)} sections to process")
        total_rules = 0
        total_links = 0
        for i, r in enumerate(rows, 1):
            section = r["section_id"]
            content = (r["content"] or "").strip()
            if len(content) < 50:
                continue
            scope = f"section:{section}"
            scope_prefix_id = f"sec{section}"
            rules = llm_decompose(scope, content)
            if not rules:
                continue
            n_ins, n_lnk = insert_rules(cur, scope, f"UK Tariff Section {section} Notes", scope_prefix_id, rules)
            total_rules += n_ins
            total_links += n_lnk
            print(f"    [{i}/{len(rows)}] Sec {section}: {n_ins} rules, {n_lnk} cross-links")
            c.commit()
        print(f"  total rules: {total_rules}, cross-links: {total_links}")


def remove_blob_edges():
    """Delete the legacy 'chXX_notes' blob edges we seeded earlier - they're
    superseded by the decomposed `chXX_note_*` edges."""
    print("=" * 60)
    print("Removing legacy blob edges (chXX_notes / secX_notes)...")
    with psycopg.connect(DSN, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute(
            "DELETE FROM kg.kg_edges WHERE id ~ '^(ch|sec)[0-9XVIxvi]+_notes$' RETURNING id"
        )
        removed = [r["id"] for r in cur.fetchall()]
        c.commit()
        print(f"  removed {len(removed)} legacy blob edges")


def main():
    if "--blob-only" in sys.argv:
        remove_blob_edges()
        return
    if "--remove-blobs" in sys.argv:
        remove_blob_edges()
    decompose_chapter_notes()
    decompose_section_notes()
    remove_blob_edges()
    # Summary
    with psycopg.connect(DSN, row_factory=dict_row) as c, c.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM kg.kg_edges WHERE id LIKE 'ch%_note_%' OR id LIKE 'sec%_note_%'")
        n_notes = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM kg.kg_edges")
        n_total = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM kg.kg_edge_commodities")
        n_links = cur.fetchone()["n"]
        print(f"\nTotal decomposed-note edges: {n_notes}")
        print(f"Total KG edges: {n_total}")
        print(f"Total edge->commodity links: {n_links}")


if __name__ == "__main__":
    main()
