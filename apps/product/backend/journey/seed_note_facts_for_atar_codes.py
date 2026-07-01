"""Extract note/HSEN-derived facts for ATAR-backed commodity codes.

This is a focused KG enrichment pass for the ATAR evaluation slice:

- target codes: distinct commodity codes with existing `source LIKE 'atar:%'`
  facts in `kg.commodity_facets`;
- fact sources: UK tariff section/chapter notes and HSEN explanatory-note
  edges already present in the local KG;
- rule sources: decomposed UK tariff note rules already present in
  `kg.kg_edges`, linked into the target code neighbourhood.

Provider calls are deliberately gated. A dry run and a rule-link-only pass do
not call the provider.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

for envp in (
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
    Path(__file__).parent.parent / ".env",
    Path(__file__).parent.parent.parent / ".env",
):
    if envp is not None and envp.exists():
        load_dotenv(envp)
        break

import psycopg
from psycopg.rows import dict_row

try:
    from .evidence_labels import facet_labels
except ImportError:
    from evidence_labels import facet_labels


DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
DEFAULT_MODEL = os.environ.get("NOTE_FACTS_MODEL", "gpt-5.4-nano")
DEFAULT_FALLBACK_MODEL = os.environ.get("NOTE_FACTS_FALLBACK_MODEL", "gpt-5.4")
DEFAULT_REASONING_EFFORT = os.environ.get("NOTE_FACTS_REASONING_EFFORT", "low")
DEFAULT_MAX_CONTEXT_CHARS = int(os.environ.get("NOTE_FACTS_MAX_CONTEXT_CHARS", "9000"))
DEFAULT_MAX_REFERENCE_CODES = int(os.environ.get("NOTE_RULE_MAX_REFERENCE_CODES", "250"))
DEFAULT_HEADING_NEIGHBOUR_CAP = int(os.environ.get("NOTE_RULE_HEADING_NEIGHBOUR_CAP", "120"))

UK_NOTES_SOURCE = "uk_note_llm"
HSEN_SOURCE = "hsen_llm"


EXTRACTION_PROMPT = """You are extracting structured facts about a goods commodity for an AI-assisted classification system.

Given the commodity code and its tariff descriptions, output a JSON object of structured facts as {slot: value} pairs.

Use snake_case slot names (e.g. material_upper, beverage_type, mounting). Pick values that would be useful as multiple-choice answer options when narrowing similar codes. Use short answers (1-3 words).

Only output facts that are clearly stated or strongly implied by the descriptions. Don't invent.

Examples:
  Input: 6402200000 - Footwear with upper straps or thongs assembled to the sole by means of plugs
  Output: {"material_upper": "rubber_or_plastic", "material_sole": "rubber_or_plastic", "closure": "strap_thong", "construction": "plug_assembled"}

  Input: 2204101400 - Sparkling wine of fresh grapes, of an actual alcoholic strength by volume of not less than 8.5%, in containers of a holding capacity not exceeding 2 litres
  Output: {"beverage_type": "wine", "still_or_sparkling": "sparkling", "container_size": "le_2L", "alcohol_band": "8.5_to_22"}

Respond with the JSON object only, no preamble, no markdown.
"""


@dataclass(frozen=True)
class TargetCode:
    code: str
    chapter: str
    heading: str
    section_id: int | None
    description: str
    self_text: str


@dataclass
class ExtractedBatch:
    source: str
    authority_tier: int
    facts: dict[str, str]
    evidence: str
    provenance: dict[str, Any]


def _conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def _flat(code: str) -> str:
    digits = re.sub(r"\D", "", code or "")
    return digits.ljust(10, "0")[:10] if digits else code


def _clean_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _clip(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "\n[truncated]"


def _slot(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text or len(text) > 80 or not re.match(r"^[a-z][a-z0-9_]*$", text):
        return None
    if text in {"none", "unknown", "n_a", "na", "null"}:
        return None
    return text


def _facet_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = "_or_".join(str(v) for v in value if str(v).strip())
    text = str(value).strip()
    if not text:
        return None
    text = text.lower()
    text = text.replace("<=", "le_").replace(">=", "ge_").replace("<", "lt_").replace(">", "gt_")
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^a-z0-9_.+-]+", "_", text).strip("_")
    text = re.sub(r"_+", "_", text)
    if not text or text in {"none", "unknown", "n_a", "na", "null"}:
        return None
    return text[:80]


def _parse_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    if isinstance(data, dict) and isinstance(data.get("facts"), list):
        return {
            str(item.get("slot")): item.get("value", item.get("answer"))
            for item in data["facts"]
            if isinstance(item, dict)
        }
    return data if isinstance(data, dict) else {}


def _normalise_facts(raw: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in raw.items():
        slot = _slot(key)
        facet_value = _facet_value(value)
        if slot and facet_value:
            out[slot] = facet_value
    return out


def _load_targets(limit: int | None = None) -> list[TargetCode]:
    sql = """
        WITH atar_codes AS (
          SELECT DISTINCT commodity_code AS code
          FROM kg.commodity_facets
          WHERE source LIKE 'atar:%%'
        )
        SELECT DISTINCT ON (ac.code)
               ac.code,
               substring(ac.code, 1, 2) AS chapter,
               substring(ac.code, 1, 4) AS heading,
               cn.section_id,
               COALESCE(gnd.description, '') AS description,
               COALESCE(st.self_text, '') AS self_text
        FROM atar_codes ac
        LEFT JOIN uk.goods_nomenclatures gn
          ON gn.goods_nomenclature_item_id = ac.code
         AND gn.validity_end_date IS NULL
        LEFT JOIN uk.goods_nomenclature_descriptions gnd
          ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
        LEFT JOIN uk.goods_nomenclature_self_texts st
          ON st.goods_nomenclature_item_id = ac.code
        LEFT JOIN uk.chapter_notes cn
          ON cn.chapter_id = substring(ac.code, 1, 2)
        ORDER BY ac.code, gn.goods_nomenclature_sid NULLS LAST
    """
    if limit:
        sql += " LIMIT %s"
        params: tuple[Any, ...] = (limit,)
    else:
        params = ()
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return [
            TargetCode(
                code=r["code"],
                chapter=r["chapter"],
                heading=r["heading"],
                section_id=r["section_id"],
                description=_clean_text(r["description"]),
                self_text=_clean_text(r["self_text"]),
            )
            for r in cur.fetchall()
        ]


def _uk_note_context(target: TargetCode, max_chars: int) -> tuple[str, dict[str, Any]]:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT content FROM uk.chapter_notes WHERE chapter_id = %s",
            (target.chapter,),
        )
        chapter = _clean_text((cur.fetchone() or {}).get("content"))
        section = ""
        if target.section_id is not None:
            cur.execute(
                "SELECT content FROM uk.section_notes WHERE section_id = %s",
                (target.section_id,),
            )
            section = _clean_text((cur.fetchone() or {}).get("content"))
    section_budget = max_chars // 3
    chapter_budget = max_chars - section_budget
    pieces = []
    if chapter:
        pieces.append(f"UK Tariff Chapter {target.chapter} Notes:\n{_clip(chapter, chapter_budget)}")
    if section:
        pieces.append(f"UK Tariff Section {target.section_id} Notes:\n{_clip(section, section_budget)}")
    return "\n\n".join(pieces), {
        "source_type": "uk_tariff_notes",
        "chapter": target.chapter,
        "section_id": target.section_id,
    }


def _hsen_context(target: TargetCode, max_chars: int) -> tuple[str, dict[str, Any]]:
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            SELECT e.id, e.type, e.title, e.body, e.source, e.scope
            FROM kg.kg_edges e
            JOIN kg.kg_edge_commodities kec ON kec.edge_id = e.id
            WHERE kec.commodity_code = %s
              AND e.source LIKE 'hsen:%%'
            ORDER BY CASE e.type
              WHEN 'hsen_heading' THEN 0
              WHEN 'hsen_general' THEN 1
              WHEN 'hsen_section_general' THEN 2
              ELSE 9
            END, e.id
            """,
            (target.code,),
        )
        rows = cur.fetchall()
    if not rows:
        return "", {"source_type": "hsen", "edge_ids": []}
    remaining = max_chars
    pieces: list[str] = []
    edge_ids: list[str] = []
    for r in rows:
        if remaining <= 400:
            break
        body = _clean_text(r["body"])
        budget = min(remaining, max_chars // 2 if r["type"] == "hsen_heading" else max_chars // 4)
        snippet = _clip(body, max(400, budget))
        pieces.append(f"{r['title']} ({r['id']}):\n{snippet}")
        edge_ids.append(r["id"])
        remaining -= len(snippet)
    return "\n\n".join(pieces), {"source_type": "hsen", "edge_ids": edge_ids}


def _build_user_prompt(target: TargetCode, source_label: str, context: str) -> str:
    description_lines = [d for d in (target.description, target.self_text) if d]
    if not description_lines:
        description_lines = [target.code]
    return (
        f"Input: {target.code} - {target.description or target.self_text}\n\n"
        "Tariff descriptions:\n"
        + "\n".join(f"- {line}" for line in description_lines)
        + f"\n\nAdditional source context ({source_label}). "
        "Only use this context when it clearly applies to the commodity code; "
        "ignore rules about unrelated goods, except where they define exclusions, thresholds, or discriminators for this code's neighbourhood.\n"
        f"{context}"
    )


class ExtractorClient:
    def __init__(self, model: str, fallback_model: str | None, reasoning_effort: str):
        from openai import OpenAI

        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=60.0, max_retries=3)
        self.model = model
        self.fallback_model = fallback_model
        self.reasoning_effort = reasoning_effort
        self.used_fallback = False

    def _kwargs(self, model: str, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if model.startswith(("gpt-5", "o")):
            kwargs["reasoning_effort"] = self.reasoning_effort
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["temperature"] = 0
            kwargs["max_tokens"] = max_tokens
        return kwargs

    def complete_json(self, user_prompt: str, max_tokens: int = 800) -> tuple[dict[str, Any], str]:
        messages = [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        model = self.model
        for attempt in range(3):
            try:
                resp = self.client.chat.completions.create(**self._kwargs(model, messages, max_tokens))
                text = resp.choices[0].message.content or "{}"
                return _parse_json_object(text), model
            except Exception as exc:
                msg = str(exc)
                can_fallback = (
                    not self.used_fallback
                    and self.fallback_model
                    and self.fallback_model != model
                    and re.search(r"model|not found|does not exist|unsupported", msg, re.I)
                )
                if can_fallback:
                    print(f"  provider rejected model {model!r}; falling back to {self.fallback_model!r}")
                    model = self.fallback_model
                    self.model = model
                    self.used_fallback = True
                    continue
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))
        return {}, model

    def probe(self) -> str:
        data, model = self.complete_json('Return exactly {"ok": true}.', max_tokens=80)
        if data.get("ok") is not True:
            raise RuntimeError(f"unexpected probe payload from {model}: {data}")
        return model


def _source_existing(cur, code: str, source: str) -> int:
    cur.execute(
        "SELECT count(*) AS n FROM kg.commodity_facets WHERE commodity_code = %s AND source = %s",
        (code, source),
    )
    return int((cur.fetchone() or {}).get("n") or 0)


def _delete_source_facts(cur, code: str, source: str) -> int:
    cur.execute(
        "DELETE FROM kg.commodity_facets WHERE commodity_code = %s AND source = %s RETURNING id",
        (code, source),
    )
    return len(cur.fetchall())


def _insert_fact(cur, target: TargetCode, source: str, key: str, value: str, evidence: str, authority_tier: int, provenance: dict[str, Any]) -> None:
    scopes, roles = facet_labels(source, key, value)
    if source in {UK_NOTES_SOURCE, HSEN_SOURCE}:
        # These are commodity attributes extracted from legal/interpretive note
        # context for classification. Keep them out of duty/declaration/landed
        # cost roles even when a slot name contains incidental substrings like
        # "vat" inside "preservation".
        scopes = ["retrieval", "classification", "qa", "audit"]
        non_commodity_roles = {
            "measure_condition",
            "document_requirement",
            "duty_rate_measure",
            "valuation_input",
            "valuation_method",
            "declaration_data",
        }
        roles = [r for r in roles if r not in non_commodity_roles and r != "landed_cost_component"]
        if not roles:
            if source == UK_NOTES_SOURCE:
                roles = ["product_identity", "legal_definition"]
            else:
                roles = ["product_identity", "interpretive_guidance"]
    cur.execute(
        """
        INSERT INTO kg.facet_definitions (key, label, applies_to_chapters)
        VALUES (%s, %s, ARRAY[%s]::text[])
        ON CONFLICT (key) DO UPDATE SET
          applies_to_chapters = (
            SELECT array_agg(DISTINCT x)
            FROM unnest(COALESCE(kg.facet_definitions.applies_to_chapters, ARRAY[]::text[]) || ARRAY[%s]::text[]) AS x
          )
        """,
        (key, key.replace("_", " ").capitalize(), target.chapter, target.chapter),
    )
    cur.execute(
        """
        INSERT INTO kg.commodity_facets
          (commodity_code, facet_key, facet_value, source, confidence, evidence,
           authority_tier, use_scopes, evidence_roles, provenance)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s::text[], %s::jsonb)
        ON CONFLICT (commodity_code, facet_key, facet_value, source) DO UPDATE SET
          confidence = EXCLUDED.confidence,
          evidence = EXCLUDED.evidence,
          authority_tier = EXCLUDED.authority_tier,
          use_scopes = EXCLUDED.use_scopes,
          evidence_roles = EXCLUDED.evidence_roles,
          provenance = EXCLUDED.provenance,
          embedding_stale = true,
          updated_at = now()
        """,
        (
            target.code,
            key,
            value,
            source,
            0.82 if source == UK_NOTES_SOURCE else 0.78,
            evidence[:1000],
            authority_tier,
            scopes,
            roles,
            json.dumps(provenance),
        ),
    )


def _extract_for_target(
    client: ExtractorClient,
    target: TargetCode,
    source: str,
    max_context_chars: int,
) -> ExtractedBatch | None:
    if source == UK_NOTES_SOURCE:
        context, base_provenance = _uk_note_context(target, max_context_chars)
        source_label = "UK tariff section/chapter notes"
        tier = 1
        evidence = f"LLM extraction from UK Tariff chapter/section notes for {target.code}"
    else:
        context, base_provenance = _hsen_context(target, max_context_chars)
        source_label = "HSEN explanatory notes"
        tier = 3
        evidence = f"LLM extraction from HSEN explanatory notes linked to {target.code}"
    if not context.strip():
        return None
    prompt = _build_user_prompt(target, source_label, context)
    raw, actual_model = client.complete_json(prompt)
    facts = _normalise_facts(raw)
    if not facts:
        return None
    provenance = {
        **base_provenance,
        "extractor": "seed_note_facts_for_atar_codes",
        "extractor_model": actual_model,
        "reasoning_effort": client.reasoning_effort,
        "target_code": target.code,
        "chapter": target.chapter,
        "heading": target.heading,
        "prompt": "description_fact_extraction",
    }
    return ExtractedBatch(source=source, authority_tier=tier, facts=facts, evidence=evidence, provenance=provenance)


def extract_facts(args: argparse.Namespace) -> dict[str, int]:
    if not args.allow_spend:
        raise SystemExit("Refusing provider extraction without --allow-spend.")
    if os.environ.get("NOTES_FACTS_ALLOW_PROVIDER_CALLS") != "1":
        raise SystemExit("Refusing provider extraction without NOTES_FACTS_ALLOW_PROVIDER_CALLS=1.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not configured.")

    sources = [UK_NOTES_SOURCE, HSEN_SOURCE] if args.source == "both" else [args.source]
    targets = _load_targets(args.limit)
    client = ExtractorClient(args.model, args.fallback_model, args.reasoning_effort)
    stats = {"targets": len(targets), "calls": 0, "facts": 0, "skipped_existing": 0, "empty": 0}
    with _conn() as c, c.cursor() as cur:
        for i, target in enumerate(targets, 1):
            for source in sources:
                if args.max_calls is not None and stats["calls"] >= args.max_calls:
                    c.commit()
                    return stats
                existing = _source_existing(cur, target.code, source)
                if existing and not args.force:
                    stats["skipped_existing"] += 1
                    continue
                if args.force and existing:
                    _delete_source_facts(cur, target.code, source)
                print(f"[{i}/{len(targets)}] {target.code} {source}")
                batch = _extract_for_target(client, target, source, args.max_context_chars)
                stats["calls"] += 1
                if not batch:
                    stats["empty"] += 1
                    c.commit()
                    continue
                for key, value in batch.facts.items():
                    _insert_fact(cur, target, batch.source, key, value, batch.evidence, batch.authority_tier, batch.provenance)
                    stats["facts"] += 1
                c.commit()
                print(f"  inserted/updated {len(batch.facts)} facts")
    return stats


def _edge_references(edge: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    provenance = edge.get("provenance") or {}
    if isinstance(provenance, str):
        try:
            provenance = json.loads(provenance)
        except json.JSONDecodeError:
            provenance = {}
    for ref in provenance.get("references") or []:
        if isinstance(ref, str):
            refs.add(ref.strip())

    body = edge.get("body") or ""
    # Keep this conservative: only references explicitly introduced as chapter,
    # heading or subheading refs.
    patterns = [
        (r"\bheadings?\s+((?:\d{4}(?:\s*(?:,|and|or)\s*)?)+)", "h"),
        (r"\bsubheadings?\s+((?:\d{6}(?:\s*(?:,|and|or)\s*)?)+)", "sub"),
        (r"\bchapters?\s+((?:\d{2}(?:\s*(?:,|and|or)\s*)?)+)", "ch"),
    ]
    for pattern, kind in patterns:
        for match in re.finditer(pattern, body, flags=re.I):
            for digits in re.findall(r"\d+", match.group(1)):
                refs.add(f"{kind}:{digits}")
    return refs


def _codes_for_prefix(cur, ref: str, cap: int) -> list[str]:
    if ":" not in ref:
        return []
    kind, body = ref.split(":", 1)
    digits = re.sub(r"\D", "", body)
    if kind == "cc" and len(digits) == 10:
        return [digits]
    if kind == "sub" and len(digits) == 6:
        prefix = digits
    elif kind == "h" and len(digits) == 4:
        prefix = digits
    elif kind == "ch" and len(digits) == 2:
        prefix = digits
    else:
        return []
    cur.execute(
        """
        SELECT goods_nomenclature_item_id AS code
        FROM uk.goods_nomenclatures
        WHERE producline_suffix = '80'
          AND validity_end_date IS NULL
          AND goods_nomenclature_item_id LIKE %s
        ORDER BY goods_nomenclature_item_id
        LIMIT %s
        """,
        (prefix + "%", cap),
    )
    return [r["code"] for r in cur.fetchall()]


def _heading_neighbours(cur, heading: str, cap: int) -> list[str]:
    cur.execute(
        """
        SELECT goods_nomenclature_item_id AS code
        FROM uk.goods_nomenclatures
        WHERE producline_suffix = '80'
          AND validity_end_date IS NULL
          AND goods_nomenclature_item_id LIKE %s
        ORDER BY goods_nomenclature_item_id
        LIMIT %s
        """,
        (heading + "%", cap),
    )
    return [r["code"] for r in cur.fetchall()]


def _note_edges_for_target(cur, target: TargetCode) -> list[dict[str, Any]]:
    scopes = [f"chapter:{target.chapter}"]
    if target.section_id is not None:
        scopes.append(f"section:{target.section_id}")
    cur.execute(
        """
        SELECT id, type, scope, title, body, source, provenance
        FROM kg.kg_edges
        WHERE source LIKE 'UK Tariff %% Notes'
          AND scope = ANY(%s)
        ORDER BY id
        """,
        (scopes,),
    )
    return [dict(r) for r in cur.fetchall()]


def link_note_rules(args: argparse.Namespace) -> dict[str, int]:
    targets = _load_targets(args.limit)
    stats = {
        "targets": len(targets),
        "applicable_edges": 0,
        "target_neighbour_links": 0,
        "reference_links": 0,
    }
    with _conn() as c, c.cursor() as cur:
        for target in targets:
            edges = _note_edges_for_target(cur, target)
            stats["applicable_edges"] += len(edges)
            neighbours = set(_heading_neighbours(cur, target.heading, args.heading_neighbour_cap))
            neighbours.add(target.code)
            for edge in edges:
                refs = _edge_references(edge)
                reference_codes: set[str] = set()
                for ref in refs:
                    reference_codes.update(_codes_for_prefix(cur, ref, args.max_reference_codes))
                if not args.dry_run:
                    for code in sorted(neighbours):
                        cur.execute(
                            """
                            INSERT INTO kg.kg_edge_commodities (edge_id, commodity_code)
                            VALUES (%s, %s) ON CONFLICT DO NOTHING
                            """,
                            (edge["id"], code),
                        )
                        if cur.rowcount:
                            stats["target_neighbour_links"] += 1
                    for code in sorted(reference_codes):
                        cur.execute(
                            """
                            INSERT INTO kg.kg_edge_commodities (edge_id, commodity_code)
                            VALUES (%s, %s) ON CONFLICT DO NOTHING
                            """,
                            (edge["id"], code),
                        )
                        if cur.rowcount:
                            stats["reference_links"] += 1
                else:
                    stats["target_neighbour_links"] += len(neighbours)
                    stats["reference_links"] += len(reference_codes)
        if not args.dry_run:
            c.commit()
    return stats


def dry_run(args: argparse.Namespace) -> dict[str, int]:
    targets = _load_targets(args.limit)
    sources = [UK_NOTES_SOURCE, HSEN_SOURCE] if args.source == "both" else [args.source]
    stats = {"targets": len(targets), "candidate_calls": 0, "skipped_existing": 0, "missing_context": 0}
    with _conn() as c, c.cursor() as cur:
        for target in targets:
            for source in sources:
                if _source_existing(cur, target.code, source) and not args.force:
                    stats["skipped_existing"] += 1
                    continue
                if source == UK_NOTES_SOURCE:
                    context, _ = _uk_note_context(target, args.max_context_chars)
                else:
                    context, _ = _hsen_context(target, args.max_context_chars)
                if not context.strip():
                    stats["missing_context"] += 1
                    continue
                stats["candidate_calls"] += 1
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-spend", action="store_true")
    parser.add_argument("--force", action="store_true", help="Replace existing facts for the selected source(s).")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--source", choices=[UK_NOTES_SOURCE, HSEN_SOURCE, "both"], default="both")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--fallback-model", default=DEFAULT_FALLBACK_MODEL)
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT)
    parser.add_argument("--max-context-chars", type=int, default=DEFAULT_MAX_CONTEXT_CHARS)
    parser.add_argument("--max-reference-codes", type=int, default=DEFAULT_MAX_REFERENCE_CODES)
    parser.add_argument("--heading-neighbour-cap", type=int, default=DEFAULT_HEADING_NEIGHBOUR_CAP)
    parser.add_argument("--facts-only", action="store_true")
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--probe", action="store_true", help="Make one tiny provider call and exit.")
    args = parser.parse_args()

    if args.probe:
        if not args.allow_spend:
            raise SystemExit("Refusing provider probe without --allow-spend.")
        if os.environ.get("NOTES_FACTS_ALLOW_PROVIDER_CALLS") != "1":
            raise SystemExit("Refusing provider probe without NOTES_FACTS_ALLOW_PROVIDER_CALLS=1.")
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY is not configured.")
        actual = ExtractorClient(args.model, args.fallback_model, args.reasoning_effort).probe()
        print(json.dumps({"probe": "ok", "model": actual, "reasoning_effort": args.reasoning_effort}, indent=2))
        return

    if args.dry_run:
        result: dict[str, Any] = {}
        if not args.rules_only:
            result["facts"] = dry_run(args)
        if not args.facts_only:
            result["rule_links"] = link_note_rules(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    result: dict[str, Any] = {}
    if not args.rules_only:
        result["facts"] = extract_facts(args)
    if not args.facts_only:
        result["rule_links"] = link_note_rules(args)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
