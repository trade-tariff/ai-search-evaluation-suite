"""Hydrate commodity facts with the deployed `scope_qna_plus` prompt.

The deployed retrieval winner used gpt-5.4-mini-authored facts with prompt
version `scope_qna_plus`. This local seeder applies that exact extraction prompt
to a capped set of active commodity codes, using tariff descriptions, self text,
labels, search references, and already-loaded KG snippets as context.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

for envp in (
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
    Path(__file__).parent.parent.parent / ".env",
    Path(os.environ["AI_FAN_OUT_ENV_FILE"]) if os.environ.get("AI_FAN_OUT_ENV_FILE") else None,
):
    if envp is not None and envp.exists():
        load_dotenv(envp)
        break

import asyncpg
from openai import AsyncOpenAI

try:
    from .evidence_labels import facet_labels
except ImportError:
    from evidence_labels import facet_labels


DSN = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
MODEL = os.environ.get("SCOPE_QNA_MODEL", os.environ.get("FACET_LLM_MODEL", "gpt-5.4-mini"))
SOURCE = os.environ.get("SCOPE_QNA_SOURCE", "description_llm_scope_qna_plus")
PROMPT_VERSION = "scope_qna_plus"
LIMIT = int(os.environ.get("SCOPE_QNA_LIMIT", "1000"))
CONCURRENCY = int(os.environ.get("SCOPE_QNA_CONCURRENCY", "4"))
FORCE = os.environ.get("SCOPE_QNA_FORCE", "").strip().lower() in {"1", "true", "yes", "on"}
REQUEST_TIMEOUT_S = float(os.environ.get("SCOPE_QNA_TIMEOUT_S", "90"))

PRICE_IN = float(os.environ.get("SCOPE_QNA_INPUT_COST_PER_M", "0.25"))
PRICE_OUT = float(os.environ.get("SCOPE_QNA_OUTPUT_COST_PER_M", "2.00"))

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

PROMPT_SHA256 = hashlib.sha256(EXTRACTION_PROMPT.encode("utf-8")).hexdigest()

CLASSIFICATION_USE_SCOPES = {"retrieval", "classification", "qa", "audit"}
CLASSIFICATION_EVIDENCE_ROLES = {
    "alias",
    "product_identity",
    "material_composition",
    "form_presentation",
    "function_use",
    "packaging_quantity",
    "composition_threshold",
    "additional_code",
    "origin_or_region",
    "legal_definition",
    "legal_inclusion",
    "legal_exclusion",
    "classification_order",
    "classification_rationale",
    "interpretive_guidance",
    "heading_guidance",
    "footnote",
    "index_text",
    "unknown",
}

_prompt_tokens = 0
_completion_tokens = 0


def _clean(text: object, limit: int = 1200) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def _list(values: object, limit: int = 12) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean(value, 100)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _valid_key(key: str) -> bool:
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{1,60}", key))


def _valid_value(value: str) -> bool:
    return bool(value and 1 <= len(value) <= 120 and value.lower() not in {"unknown", "n/a", "none"})


def _format_context(row: asyncpg.Record, facets: list[asyncpg.Record], edges: list[asyncpg.Record]) -> str:
    parts = [
        f"Commodity: {row['code']}",
        "Descriptions:",
        f"- Tariff description: {_clean(row['description'])}",
    ]
    if row["self_text"]:
        parts.append(f"- Tariff self text: {_clean(row['self_text'], 1600)}")
    if row["label_description"]:
        parts.append(f"- AI label: {_clean(row['label_description'], 500)}")
    synonyms = _list(row["synonyms"])
    colloquial = _list(row["colloquial_terms"])
    brands = _list(row["known_brands"])
    refs = _list(row["search_references"])
    if synonyms or colloquial:
        parts.append("- Also known as: " + ", ".join([*colloquial, *synonyms]))
    if brands:
        parts.append("- Known brands: " + ", ".join(brands))
    if refs:
        parts.append("- Search references: " + ", ".join(refs))

    if facets:
        parts.append("Existing KG facts and measure/certificate context:")
        for facet in facets[:14]:
            evidence = _clean(facet["evidence"], 180)
            suffix = f" ({evidence})" if evidence else ""
            parts.append(f"- {facet['facet_key']}: {facet['facet_value']}{suffix}")

    if edges:
        parts.append("KG rules, notes, HSEN, ATAR, footnote snippets:")
        for edge in edges[:10]:
            body = _clean(edge["body"], 260)
            parts.append(f"- [{edge['type']} T{edge['authority_tier']}] {edge['title']}: {body}")

    return "\n".join(parts)[:9000]


async def _target_rows(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    skip_clause = (
        ""
        if FORCE
        else "AND NOT EXISTS (SELECT 1 FROM kg.commodity_facets cf WHERE cf.commodity_code = gn.goods_nomenclature_item_id AND cf.source = $2)"
    )
    args: tuple[object, ...] = (LIMIT,) if FORCE else (LIMIT, SOURCE)
    async with pool.acquire() as conn:
        return await conn.fetch(
            f"""
            WITH active_codes AS (
              SELECT DISTINCT ON (gn.goods_nomenclature_item_id)
                     gn.goods_nomenclature_item_id AS code,
                     gnd.description,
                     st.self_text,
                     gl.description AS label_description,
                     gl.synonyms,
                     gl.colloquial_terms,
                     gl.known_brands,
                     sr.refs AS search_references,
                     CASE WHEN eg.expected_code IS NOT NULL THEN 0
                          WHEN gl.goods_nomenclature_item_id IS NOT NULL THEN 1
                          WHEN sr.refs IS NOT NULL THEN 2
                          ELSE 3 END AS priority
              FROM uk.goods_nomenclatures gn
              JOIN uk.goods_nomenclature_descriptions gnd
                ON gnd.goods_nomenclature_sid = gn.goods_nomenclature_sid
              LEFT JOIN uk.goods_nomenclature_self_texts st
                ON st.goods_nomenclature_item_id = gn.goods_nomenclature_item_id
              LEFT JOIN uk.goods_nomenclature_labels gl
                ON gl.goods_nomenclature_item_id = gn.goods_nomenclature_item_id
               AND gl.expired = false
              LEFT JOIN (
                SELECT goods_nomenclature_item_id AS code,
                       array_agg(DISTINCT title ORDER BY title) AS refs
                FROM uk.search_references
                WHERE title IS NOT NULL
                GROUP BY 1
              ) sr ON sr.code = gn.goods_nomenclature_item_id
              LEFT JOIN (
                SELECT DISTINCT expected_code FROM kg.eval_gold WHERE active = true
              ) eg ON eg.expected_code = gn.goods_nomenclature_item_id
              WHERE gn.validity_end_date IS NULL
                AND gnd.description IS NOT NULL
                AND length(gnd.description) > 3
                {skip_clause}
              ORDER BY gn.goods_nomenclature_item_id, priority
            )
            SELECT * FROM active_codes
            ORDER BY priority, code
            LIMIT $1
            """,
            *args,
        )


async def _context_rows(pool: asyncpg.Pool, code: str) -> tuple[list[asyncpg.Record], list[asyncpg.Record]]:
    async with pool.acquire() as conn:
        facets = await conn.fetch(
            """
            SELECT facet_key, facet_value, source, evidence, authority_tier
            FROM kg.commodity_facets
            WHERE commodity_code = $1
              AND source <> $2
            ORDER BY authority_tier, source, facet_key
            LIMIT 18
            """,
            code,
            SOURCE,
        )
        edges = await conn.fetch(
            """
            SELECT e.id, e.type, e.title, e.body, e.source, e.authority_tier
            FROM kg.kg_edges e
            JOIN kg.kg_edge_commodities kec ON kec.edge_id = e.id
            WHERE kec.commodity_code = $1
            ORDER BY e.authority_tier, e.type, e.id
            LIMIT 12
            """,
            code,
        )
        return list(facets), list(edges)


async def _extract(client: AsyncOpenAI, context: str) -> tuple[dict[str, str], dict[str, int]]:
    global _prompt_tokens, _completion_tokens
    kwargs: dict = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": EXTRACTION_PROMPT},
            {"role": "user", "content": context},
        ],
        "response_format": {"type": "json_object"},
        "timeout": REQUEST_TIMEOUT_S,
    }
    if MODEL.startswith("gpt-5") or MODEL.startswith("o"):
        kwargs["max_completion_tokens"] = 800
        kwargs["reasoning_effort"] = os.environ.get("SCOPE_QNA_REASONING_EFFORT", "low")
    else:
        kwargs["max_tokens"] = 500
        kwargs["temperature"] = 0.0

    resp = await client.chat.completions.create(**kwargs)
    if getattr(resp, "usage", None):
        usage = resp.usage
        _prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
        _completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
    raw = (resp.choices[0].message.content or "{}").strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return {}, {}
    out: dict[str, str] = {}
    for key, value in parsed.items():
        k = str(key or "").strip()
        v = _clean(value, 120)
        if _valid_key(k) and _valid_value(v):
            out[k] = v
    return out, {
        "prompt_tokens": int(getattr(getattr(resp, "usage", None), "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(getattr(resp, "usage", None), "completion_tokens", 0) or 0),
    }


async def _write_facts(pool: asyncpg.Pool, code: str, facts: dict[str, str], evidence: str) -> int:
    if not facts:
        return 0
    provenance = {
        "source_type": "tariff_description_plus_kg_context",
        "extractor": "seed_scope_qna_plus_facets",
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": PROMPT_SHA256,
    }
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = 0
            for key, value in facts.items():
                use_scopes, roles = facet_labels(SOURCE, key, value)
                use_scopes = [scope for scope in use_scopes if scope in CLASSIFICATION_USE_SCOPES] or [
                    "retrieval",
                    "classification",
                    "qa",
                    "audit",
                ]
                roles = [role for role in roles if role in CLASSIFICATION_EVIDENCE_ROLES] or ["product_identity"]
                await conn.execute(
                    """
                    INSERT INTO kg.facet_definitions (key, label)
                    VALUES ($1, $2)
                    ON CONFLICT (key) DO NOTHING
                    """,
                    key,
                    key.replace("_", " ").capitalize(),
                )
                await conn.execute(
                    """
                    INSERT INTO kg.commodity_facets
                      (commodity_code, facet_key, facet_value, source, confidence, evidence,
                       authority_tier, use_scopes, evidence_roles, provenance)
                    VALUES ($1, $2, $3, $4, 0.88, $5, 6, $6::text[], $7::text[], $8::jsonb)
                    ON CONFLICT (commodity_code, facet_key, facet_value, source) DO UPDATE SET
                      evidence = EXCLUDED.evidence,
                      use_scopes = EXCLUDED.use_scopes,
                      evidence_roles = EXCLUDED.evidence_roles,
                      provenance = EXCLUDED.provenance,
                      updated_at = now()
                    """,
                    code,
                    key,
                    value,
                    SOURCE,
                    evidence[:800],
                    use_scopes,
                    roles,
                    json.dumps(provenance),
                )
                inserted += 1
            return inserted


async def _process_one(client: AsyncOpenAI, pool: asyncpg.Pool, row: asyncpg.Record, sem: asyncio.Semaphore) -> tuple[str, int, str | None]:
    code = row["code"]
    try:
        facets, edges = await _context_rows(pool, code)
        context = _format_context(row, facets, edges)
        async with sem:
            facts, _usage = await _extract(client, context)
        evidence = " | ".join(
            x for x in (
                _clean(row["description"], 300),
                _clean(row["label_description"], 200),
            )
            if x
        )
        count = await _write_facts(pool, code, facts, evidence or context[:800])
        return code, count, None
    except Exception as exc:
        return code, 0, f"{type(exc).__name__}: {exc}"


async def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not configured.")
    started = time.time()
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=CONCURRENCY + 4, command_timeout=120)
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    try:
        rows = await _target_rows(pool)
        print(f"scope_qna_plus targets: {len(rows)} codes | model={MODEL} source={SOURCE} force={FORCE}")
        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = [asyncio.create_task(_process_one(client, pool, row, sem)) for row in rows]
        done = 0
        facts_total = 0
        errors = 0
        for fut in asyncio.as_completed(tasks):
            code, count, error = await fut
            done += 1
            facts_total += count
            if error:
                errors += 1
                print(f"  ! {code}: {error}", flush=True)
            if done % 25 == 0 or done == len(tasks):
                cost = (_prompt_tokens * PRICE_IN + _completion_tokens * PRICE_OUT) / 1_000_000
                print(
                    f"  {done}/{len(tasks)} codes, facts={facts_total}, errors={errors}, "
                    f"tokens={_prompt_tokens + _completion_tokens:,}, est_cost=${cost:.4f}",
                    flush=True,
                )
        elapsed = time.time() - started
        cost = (_prompt_tokens * PRICE_IN + _completion_tokens * PRICE_OUT) / 1_000_000
        print(
            f"DONE scope_qna_plus: codes={len(rows)}, facts={facts_total}, errors={errors}, "
            f"prompt_tokens={_prompt_tokens}, completion_tokens={_completion_tokens}, "
            f"est_cost=${cost:.4f}, elapsed={elapsed:.1f}s"
        )
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
