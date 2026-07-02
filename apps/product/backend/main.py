from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response

from auth import install_optional_auth
from benchmark import (
    cancel_current_run,
    get_current_run,
    list_saved_runs,
    load_saved_run,
    run_benchmark,
)
from config import load_config, save_config
from judge import detect_response_type
from prompts import get_prompt_detail, list_prompts
import complexity_charts
import kg
from schemas import (
    AppConfig,
    BenchmarkRequest,
    JudgeConfig,
    ModelConfig,
    ReferenceConfig,
    ScoringWeights,
    SimulatorConfig,
)

app = FastAPI(title="AI Fan-Out - Import Classification")

_cors_origins = [
    origin.strip()
    for origin in os.environ.get(
        "AI_FAN_OUT_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
install_optional_auth(app)

RESULTS_DIR = Path(__file__).parent.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def _workbench_spend_enabled(payload: dict | object | None = None) -> bool:
    # The operator switch is the master: with it off, no product-route spend,
    # regardless of what the request claims (the UI used to hardcode
    # allow_spend=true on several calls). With it on, an explicit
    # allow_spend=false in the request is still honoured as a refusal.
    if os.environ.get("AI_FAN_OUT_WORKBENCH_SPEND_ENABLED", "").strip() != "1":
        return False
    if isinstance(payload, dict):
        return payload.get("allow_spend", True) is not False
    return getattr(payload, "allow_spend", True) is not False


def _require_workbench_spend(payload: dict | object | None = None) -> None:
    if not _workbench_spend_enabled(payload):
        raise HTTPException(
            403,
            "Provider-backed workbench action blocked. Spend is disabled on "
            "this server; set AI_FAN_OUT_WORKBENCH_SPEND_ENABLED=1 to enable it.",
        )


# --- Config ---


@app.get("/api/config")
def api_get_config():
    cfg = load_config()
    # Mask API keys for frontend display
    keys = cfg.api_keys.model_dump()
    masked = {}
    for k, v in keys.items():
        if v and len(v) > 8:
            masked[k] = v[:4] + "..." + v[-4:]
        elif v:
            masked[k] = "***"
        else:
            masked[k] = None
    return {
        "api_keys": masked,
        "api_keys_set": {k: bool(v) for k, v in keys.items()},
        "models": [m.model_dump() for m in cfg.models],
        "judge_config": cfg.judge_config.model_dump(),
        "simulator_config": cfg.simulator_config.model_dump(),
        "reference_config": cfg.reference_config.model_dump(),
        "scoring_weights": cfg.scoring_weights.model_dump(),
        "default_selected_model_ids": cfg.default_selected_model_ids,
    }


@app.put("/api/config")
def api_update_config(payload: dict):
    cfg = load_config()

    if "api_keys" in payload:
        current = cfg.api_keys.model_dump()
        for k, v in payload["api_keys"].items():
            if v is not None and "..." not in str(v):
                current[k] = v
        from schemas import ApiKeys
        cfg.api_keys = ApiKeys(**current)

    if "models" in payload:
        cfg.models = [ModelConfig(**m) for m in payload["models"]]

    if "judge_config" in payload:
        current_judge = cfg.judge_config.model_dump()
        current_judge.update(payload["judge_config"])
        cfg.judge_config = JudgeConfig(**current_judge)

    if "simulator_config" in payload:
        current_sim = cfg.simulator_config.model_dump()
        current_sim.update(payload["simulator_config"])
        cfg.simulator_config = SimulatorConfig(**current_sim)

    if "reference_config" in payload:
        current_ref = cfg.reference_config.model_dump()
        current_ref.update(payload["reference_config"])
        cfg.reference_config = ReferenceConfig(**current_ref)

    if "scoring_weights" in payload:
        current_sw = cfg.scoring_weights.model_dump()
        current_sw.update(payload["scoring_weights"])
        cfg.scoring_weights = ScoringWeights(**current_sw)

    if "default_selected_model_ids" in payload:
        ids = payload["default_selected_model_ids"]
        if isinstance(ids, list):
            cfg.default_selected_model_ids = [str(x) for x in ids]

    save_config(cfg)
    return {"status": "ok"}


# --- Prompts ---


@app.get("/api/prompts")
def api_list_prompts():
    return list_prompts()


@app.get("/api/sections")
def api_list_sections():
    """OTT section taxonomy for the UI section filter."""
    from sections import all_sections
    return all_sections()


# --- Prompt authoring: retrieve top-N candidates for a new query ---


class PreviewRequest(dict):
    pass


@app.get("/api/search/probe")
async def api_search_probe():
    """DB reachability check for the prompt-authoring UI."""
    from search import probe_db
    return await probe_db()


@app.post("/api/search/preview")
async def api_search_preview(payload: dict):
    """Retrieve top-N candidates for a raw query via pgvector.

    Request: {"raw_query": str, "limit": int=80}
    Response: {"raw_query": str, "processed_query": str, "formatted_results": [...]}
    Shape matches search_contexts.json so the result can be saved directly.
    """
    from openai import AsyncOpenAI
    from search import retrieve_candidates

    raw_query = str(payload.get("raw_query", "")).strip()
    if not raw_query:
        raise HTTPException(400, "raw_query is required")
    limit = int(payload.get("limit", 80))
    limit = max(10, min(200, limit))

    cfg = load_config()
    if not cfg.api_keys.openai:
        raise HTTPException(400, "OpenAI API key not configured (needed for embedding)")
    _require_workbench_spend(payload)

    client = AsyncOpenAI(api_key=cfg.api_keys.openai)
    candidates = await retrieve_candidates(client, raw_query, limit=limit)
    return {
        "raw_query": raw_query,
        "processed_query": raw_query,
        "formatted_results": candidates,
        "result_count": len(candidates),
    }


# --- Retrieval experiment catalog + fresh-pair trial ---


@app.get("/api/retrieval/experiments")
def api_retrieval_experiments():
    from experiment_retrieval import experiment_catalog
    return {"experiments": experiment_catalog()}


@app.get("/api/retrieval/top-experiment")
def api_retrieval_top_experiment():
    from experiment_retrieval import top_experiment_info
    return top_experiment_info()


@app.post("/api/retrieval/try")
def api_retrieval_try(payload: dict):
    from experiment_retrieval import experiment_requires_provider, run_trial

    query = str(payload.get("query") or "").strip()
    expected_code = str(payload.get("expected_code") or "").strip()
    run_label = payload.get("run_label")
    try:
        retrieval_limit = int(payload.get("retrieval_limit") or 500)
    except (TypeError, ValueError):
        raise HTTPException(400, "retrieval_limit must be an integer")

    if experiment_requires_provider(str(run_label) if run_label else None):
        _require_workbench_spend(payload)

    cfg = load_config()
    try:
        return run_trial(
            query=query,
            expected_code=expected_code,
            api_key=cfg.api_keys.openai,
            run_label=str(run_label) if run_label else None,
            retrieval_limit=retrieval_limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        detail = f"Retrieval trial failed: {type(exc).__name__}: {str(exc)[:220]}"
        raise HTTPException(503, detail)


# --- Hydrated Q&A for retrieved candidates -----------------------------

_NONE_OPTION = "None of these are close; keep the broader shortlist"


def _code_dotted(code: str) -> str:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(digits) == 10:
        return f"{digits[:4]}.{digits[4:6]}.{digits[6:8]}.{digits[8:]}"
    return str(code or "")


def _candidate_code(candidate: dict) -> str:
    return str(candidate.get("commodity_code") or candidate.get("code") or "")


def _allows_scope(row: dict, scope: str) -> bool:
    scopes = row.get("use_scopes") or []
    return not scopes or scope in scopes


def _visible_text(value: object) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in str(value or ""))


def _clean_label(value: object, limit: int = 120) -> str:
    text = " ".join(_visible_text(value).replace("_", " ").split())
    if len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text[:1].upper() + text[1:] if text else ""


def _normal_text(value: object) -> str:
    return " ".join(_visible_text(value).lower().replace("_", " ").split())


def _normal_key(value: object) -> str:
    return "".join(ch for ch in _normal_text(value) if ch.isalnum())


def _question_mode(payload: dict) -> str:
    raw = str(payload.get("question_mode") or "facet_rules").strip().lower().replace("-", "_")
    aliases = {
        "facets": "facet_rules",
        "facet": "facet_rules",
        "deterministic": "facet_rules",
        "facet_rules": "facet_rules",
        "llm_wording": "facet_rules_llm_wording",
        "facet_rules_llm": "facet_rules_llm_wording",
        "facet_rules_llm_wording": "facet_rules_llm_wording",
        "llm": "llm_generated",
        "llm_generated": "llm_generated",
    }
    return aliases.get(raw, "facet_rules")


def _facet_priority(key: str) -> int:
    lowered = key.lower()
    if any(marker in lowered for marker in ("exclusion", "chapter_note", "section_note", "heading_rule", "legal_scope")):
        return 0
    if any(marker in lowered for marker in ("product", "type", "common_name", "category", "function", "use", "purpose")):
        return 1
    if any(marker in lowered for marker in ("material", "composition", "ingredient", "component", "substance")):
        return 2
    if any(marker in lowered for marker in ("form", "state", "processing", "prepared", "presentation", "powder", "liquid", "solid")):
        return 3
    if any(marker in lowered for marker in ("content", "protein", "fat", "sugar", "alcohol", "abv", "concentration", "starch", "glucose")):
        return 4
    if any(marker in lowered for marker in ("package", "packing", "net", "weight", "volume", "size", "container")):
        return 5
    return 6


def _question_for_facet(facet_key: str, label: str) -> str:
    lowered = facet_key.lower()
    if any(marker in lowered for marker in ("material", "composition", "ingredient", "component", "substance")):
        return "What are the goods mainly made from?"
    if any(marker in lowered for marker in ("function", "use", "purpose")):
        return "What are the goods mainly used for?"
    if any(marker in lowered for marker in ("form", "state", "processing", "presentation")):
        return "What form are the goods in?"
    if any(marker in lowered for marker in ("content", "protein", "fat", "sugar", "alcohol", "abv", "concentration")):
        return "Which composition detail best matches the goods?"
    if any(marker in lowered for marker in ("package", "packing", "net", "weight", "volume", "size", "container")):
        return "How are the goods presented or packed?"
    clean = _clean_label(label or facet_key, 80).lower()
    return f"Which {clean} best describes the goods?"


def _build_evidence(view: dict) -> tuple[list[dict], dict[str, int]]:
    evidence: list[dict] = []
    facets = view.get("facets") or []
    edges = view.get("edges") or []
    for facet in facets:
        label = facet.get("facet_label") or facet.get("facet_key") or "Facet"
        value = facet.get("facet_value") or ""
        evidence.append({
            "kind": "facet",
            "id": str(facet.get("id") or f"{label}:{value}"),
            "title": f"{label}: {value}",
            "body": str(facet.get("evidence") or value),
            "source": str(facet.get("source") or "kg.commodity_facets"),
            "scope": "qa" if _allows_scope(facet, "qa") else "retrieval",
        })
    for edge in edges[:20]:
        evidence.append({
            "kind": "kg_edge",
            "id": str(edge.get("id") or edge.get("title") or "kg_edge"),
            "title": str(edge.get("title") or edge.get("id") or "KG evidence"),
            "body": str(edge.get("body") or ""),
            "source": str(edge.get("source") or "kg.kg_edges"),
            "scope": str(edge.get("scope") or ""),
        })
    return evidence, {"facet": len(facets), "kg_edge": len(edges)}


_HYDRATION_CACHE_VERSION = "hydrated-qna-v1"
_FACT_SHEET_VERSION = "commodity-fact-sheet-v5"


async def _ensure_hydration_cache() -> None:
    pool = await kg._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {kg.KG_SCHEMA}.commodity_hydration_cache (
              commodity_code text PRIMARY KEY,
              cache_version text NOT NULL,
              payload jsonb NOT NULL,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS commodity_hydration_cache_updated_at_idx
            ON {kg.KG_SCHEMA}.commodity_hydration_cache (updated_at DESC)
            """
        )




async def _ensure_fact_sheet_kg() -> None:
    pool = await kg._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {kg.KG_SCHEMA}.commodity_fact_sheets (
              commodity_code text PRIMARY KEY,
              fact_sheet_version text NOT NULL,
              fact_sheet jsonb NOT NULL,
              label_count integer NOT NULL DEFAULT 0,
              signal_count integer NOT NULL DEFAULT 0,
              counts jsonb NOT NULL DEFAULT '{{}}'::jsonb,
              construction jsonb NOT NULL DEFAULT '{{}}'::jsonb,
              source text NOT NULL DEFAULT 'hydrated_qna_fact_sheet',
              active boolean NOT NULL DEFAULT true,
              created_at timestamptz NOT NULL DEFAULT now(),
              updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS commodity_fact_sheets_version_idx
            ON {kg.KG_SCHEMA}.commodity_fact_sheets (fact_sheet_version)
            """
        )
        await conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS commodity_fact_sheets_updated_at_idx
            ON {kg.KG_SCHEMA}.commodity_fact_sheets (updated_at DESC)
            """
        )


async def _write_fact_sheet_kg(hydration: dict) -> None:
    fact_sheet = hydration.get("fact_sheet") if isinstance(hydration.get("fact_sheet"), dict) else None
    if not fact_sheet:
        return
    code = "".join(ch for ch in str(fact_sheet.get("commodity_code") or hydration.get("commodity_code") or "") if ch.isdigit()).ljust(10, "0")[:10]
    if not code:
        return
    counts = fact_sheet.get("counts") if isinstance(fact_sheet.get("counts"), dict) else {}
    construction = fact_sheet.get("construction") if isinstance(fact_sheet.get("construction"), dict) else {}
    await _ensure_fact_sheet_kg()
    pool = await kg._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO {kg.KG_SCHEMA}.commodity_fact_sheets
              (commodity_code, fact_sheet_version, fact_sheet, label_count, signal_count, counts, construction, source, active, created_at, updated_at)
            VALUES ($1, $2, $3::jsonb, $4, $5, $6::jsonb, $7::jsonb, $8, true, now(), now())
            ON CONFLICT (commodity_code) DO UPDATE
              SET fact_sheet_version = EXCLUDED.fact_sheet_version,
                  fact_sheet = EXCLUDED.fact_sheet,
                  label_count = EXCLUDED.label_count,
                  signal_count = EXCLUDED.signal_count,
                  counts = EXCLUDED.counts,
                  construction = EXCLUDED.construction,
                  source = EXCLUDED.source,
                  active = true,
                  updated_at = now()
            """,
            code,
            str(fact_sheet.get("version") or _FACT_SHEET_VERSION),
            json.dumps(fact_sheet, default=str),
            int(counts.get("labels") or len(fact_sheet.get("labels") or [])),
            int(counts.get("signals") or len(fact_sheet.get("signals") or [])),
            json.dumps(counts, default=str),
            json.dumps(construction, default=str),
            "hydrated_qna_fact_sheet",
        )

async def _load_hydration_cache(codes: list[str]) -> dict[str, dict]:
    flats = ["".join(ch for ch in str(code or "") if ch.isdigit()).ljust(10, "0")[:10] for code in codes if code]
    if not flats:
        return {}
    await _ensure_hydration_cache()
    pool = await kg._get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT commodity_code, payload
            FROM {kg.KG_SCHEMA}.commodity_hydration_cache
            WHERE commodity_code = ANY($1::text[])
              AND cache_version = $2
            """,
            flats,
            _HYDRATION_CACHE_VERSION,
        )
    cached: dict[str, dict] = {}
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        elif not isinstance(payload, dict):
            payload = json.loads(json.dumps(payload, default=str))
        cached[row["commodity_code"]] = payload
    return cached


async def _write_hydration_cache(hydration: dict) -> None:
    code = "".join(ch for ch in str(hydration.get("commodity_code") or "") if ch.isdigit()).ljust(10, "0")[:10]
    if not code:
        return
    await _ensure_hydration_cache()
    pool = await kg._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO {kg.KG_SCHEMA}.commodity_hydration_cache
              (commodity_code, cache_version, payload, created_at, updated_at)
            VALUES ($1, $2, $3::jsonb, now(), now())
            ON CONFLICT (commodity_code) DO UPDATE
              SET cache_version = EXCLUDED.cache_version,
                  payload = EXCLUDED.payload,
                  updated_at = now()
            """,
            code,
            _HYDRATION_CACHE_VERSION,
            json.dumps(hydration, default=str),
        )
    await _write_fact_sheet_kg(hydration)


def _text_snippet(value: object, limit: int = 2200) -> str:
    text = _clean_label(value, limit)
    return text


def _store_longest_text(payload: dict, key: str, value: object, limit: int = 2200) -> None:
    text = _text_snippet(value, limit)
    if text and len(text) > len(str(payload.get(key) or "")):
        payload[key] = text


def _source_list(value: object, limit: int = 10, text_limit: int = 140) -> list[str]:
    if not value:
        return []
    raw_items = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = _clean_label(item, text_limit)
        key = _normal_key(text)
        if not text or len(key) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _append_source_list(payload: dict, key: str, value: object, limit: int = 10, text_limit: int = 180) -> None:
    items = payload.setdefault(key, [])
    if not isinstance(items, list):
        items = []
        payload[key] = items
    seen = {_normal_key(item) for item in items}
    for text in _source_list(value, limit=limit, text_limit=text_limit):
        norm = _normal_key(text)
        if norm in seen:
            continue
        seen.add(norm)
        items.append(text)
        if len(items) >= limit:
            break


def _ancestor_path_from_input_context(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return _source_list(value, limit=1, text_limit=180)
    labels: list[str] = []

    def add(text: object) -> None:
        clean = _clean_label(text, 180)
        if clean and clean.lower() not in {"true", "false", "commodity", "heading", "subheading"}:
            labels.append(clean)

    def walk(node: object) -> None:
        if isinstance(node, dict):
            add(node.get("self_text") or node.get("description"))
            return
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if isinstance(node, str):
            add(node)

    walk(value)
    return _source_list(labels, limit=12, text_limit=180)


async def _load_source_texts(codes: list[str]) -> dict[str, dict]:
    flats = sorted({"".join(ch for ch in str(code or "") if ch.isdigit()).ljust(10, "0")[:10] for code in codes if code})
    if not flats:
        return {}
    out: dict[str, dict] = {code: {} for code in flats}
    pool = await kg._get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                f"""
                SELECT goods_nomenclature_item_id AS code, self_text, search_text, input_context
                FROM {kg.TARIFF_SCHEMA}.goods_nomenclature_self_texts
                WHERE goods_nomenclature_item_id = ANY($1::text[])
                ORDER BY goods_nomenclature_item_id,
                         length(coalesce(search_text, '')) DESC,
                         length(coalesce(self_text, '')) DESC
                """,
                flats,
            )
            for row in rows:
                payload = out[row["code"]]
                _store_longest_text(payload, "self_text", row.get("self_text"), 2200)
                _store_longest_text(payload, "search_text", row.get("search_text"), 2200)
                _append_source_list(payload, "ancestor_path", _ancestor_path_from_input_context(row.get("input_context")), limit=12, text_limit=180)
        except Exception as exc:
            print(f"[fact sheet source texts] self_text unavailable: {type(exc).__name__}: {exc}")
        try:
            rows = await conn.fetch(
                f"""
                SELECT goods_nomenclature_item_id AS code,
                       description,
                       original_description,
                       synonyms,
                       colloquial_terms,
                       known_brands
                FROM {kg.TARIFF_SCHEMA}.goods_nomenclature_labels
                WHERE goods_nomenclature_item_id = ANY($1::text[])
                """,
                flats,
            )
            for row in rows:
                labels = {
                    "description": _text_snippet(row.get("description"), 500),
                    "original_description": _text_snippet(row.get("original_description"), 500),
                    "synonyms": _source_list(row.get("synonyms"), limit=10, text_limit=120),
                    "colloquial_terms": _source_list(row.get("colloquial_terms"), limit=10, text_limit=120),
                    "known_brands": _source_list(row.get("known_brands"), limit=8, text_limit=120),
                }
                out[row["code"]]["goods_labels"] = {key: value for key, value in labels.items() if value}
        except Exception as exc:
            print(f"[fact sheet source texts] goods_nomenclature_labels unavailable: {type(exc).__name__}: {exc}")
        try:
            rows = await conn.fetch(
                f"""
                SELECT goods_nomenclature_item_id AS code,
                       array_agg(DISTINCT title ORDER BY title) AS titles
                FROM {kg.TARIFF_SCHEMA}.search_references
                WHERE goods_nomenclature_item_id = ANY($1::text[])
                GROUP BY goods_nomenclature_item_id
                """,
                flats,
            )
            for row in rows:
                refs = _source_list(row.get("titles"), limit=14, text_limit=120)
                if refs:
                    out[row["code"]]["search_references"] = refs
        except Exception as exc:
            print(f"[fact sheet source texts] search_references unavailable: {type(exc).__name__}: {exc}")
        try:
            rows = await conn.fetch(
                f"""
                SELECT goods_nomenclature_item_id AS code, composite_text
                FROM {kg.KG_SCHEMA}.composite_search_text
                WHERE goods_nomenclature_item_id = ANY($1::text[])
                """,
                flats,
            )
            for row in rows:
                text = _text_snippet(row.get("composite_text"))
                if text:
                    out[row["code"]]["composite_text"] = text
        except Exception as exc:
            print(f"[fact sheet source texts] composite_text unavailable: {type(exc).__name__}: {exc}")
        try:
            rows = await conn.fetch(
                f"""
                SELECT commodity_code AS code, search_text
                FROM {kg.KG_SCHEMA}.measure_retrieval_docs
                WHERE commodity_code = ANY($1::text[])
                """,
                flats,
            )
            for row in rows:
                _store_longest_text(out[row["code"]], "measure_context", row.get("search_text"), 1400)
        except Exception as exc:
            print(f"[fact sheet source texts] measure_retrieval_docs unavailable: {type(exc).__name__}: {exc}")
        try:
            rows = await conn.fetch(
                f"""
                SELECT DISTINCT fagn.goods_nomenclature_item_id AS code,
                       fagn.footnote_type,
                       fagn.footnote_id,
                       fd.description
                FROM {kg.TARIFF_SCHEMA}.footnote_association_goods_nomenclatures_oplog fagn
                LEFT JOIN {kg.TARIFF_SCHEMA}.footnote_descriptions_oplog fd
                  ON fd.footnote_type_id = fagn.footnote_type
                 AND fd.footnote_id = fagn.footnote_id
                WHERE fagn.goods_nomenclature_item_id = ANY($1::text[])
                  AND fagn.validity_end_date IS NULL
                ORDER BY fagn.goods_nomenclature_item_id, fagn.footnote_type, fagn.footnote_id
                """,
                flats,
            )
            for row in rows:
                payload = out[row["code"]]
                notes = payload.setdefault("direct_footnotes", [])
                if not isinstance(notes, list) or len(notes) >= 6:
                    continue
                description = _clean_label(str(row.get("description") or "").replace("<br>", " "), 500)
                if not description:
                    continue
                note = {
                    "type": _clean_label(row.get("footnote_type"), 20),
                    "id": _clean_label(row.get("footnote_id"), 20),
                    "description": description,
                }
                if note not in notes:
                    notes.append(note)
        except Exception as exc:
            print(f"[fact sheet source texts] direct footnotes unavailable: {type(exc).__name__}: {exc}")
        try:
            rows = await conn.fetch(
                f"""
                SELECT commodity_code AS code,
                       scope,
                       fact_key,
                       fact_value,
                       label,
                       source,
                       evidence,
                       confidence,
                       qna_usefulness,
                       question_hint,
                       model,
                       prompt_version,
                       run_id
                FROM {kg.KG_SCHEMA}.commodity_llm_facts
                WHERE commodity_code = ANY($1::text[])
                  AND status = 'active'
                ORDER BY commodity_code,
                         CASE scope WHEN 'qna' THEN 0 WHEN 'identity' THEN 1 WHEN 'classification' THEN 2 WHEN 'exclusion' THEN 3 ELSE 4 END,
                         qna_usefulness DESC,
                         confidence DESC
                """,
                flats,
            )
            for row in rows:
                facts = out[row["code"]].setdefault("llm_facts", [])
                if not isinstance(facts, list):
                    facts = []
                    out[row["code"]]["llm_facts"] = facts
                facts.append({
                    "scope": row.get("scope"),
                    "key": row.get("fact_key"),
                    "value": row.get("fact_value"),
                    "label": row.get("label"),
                    "source": row.get("source"),
                    "evidence": row.get("evidence"),
                    "confidence": float(row.get("confidence") or 0.0),
                    "qna_usefulness": float(row.get("qna_usefulness") or 0.0),
                    "question_hint": row.get("question_hint") or "",
                    "model": row.get("model"),
                    "prompt_version": row.get("prompt_version"),
                    "run_id": row.get("run_id"),
                })
        except Exception as exc:
            print(f"[fact sheet source texts] commodity_llm_facts unavailable: {type(exc).__name__}: {exc}")
    return {code: payload for code, payload in out.items() if payload}



def _qa_option(candidate: dict, view: dict) -> str:
    priority = {
        "product type", "product_type", "common name", "common_name",
        "form", "material", "main material", "composition", "use", "function",
    }
    for facet in view.get("facets") or []:
        key = str(facet.get("facet_key") or "").lower()
        value = _clean_label(facet.get("facet_value"))
        if value and key in priority and _allows_scope(facet, "qa"):
            return value[:110]
    desc = _clean_label(candidate.get("description") or view.get("description"))
    return desc[:110] if desc else "A different product type"



_PRODUCT_FAMILY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("soap", "surface-active", "detergent", "washing preparation"), "Soap, detergent, or surface-active cleaning product"),
    (("bread", "pastry", "cake", "cakes", "biscuit", "biscuits", "bakers' wares", "communion wafer", "rice paper"), "Bread, pastry, cake, biscuit, or similar baker's ware"),
    (("muesli", "cereal flakes", "bulgur", "cereal", "wheat", "groats", "meal", "starch"), "Cereal preparation, muesli, or bulgur"),
    (("chocolate", "cocoa", "sucrose", "sugar", "glucose", "isoglucose", "sweetened"), "Sweetened or cocoa/chocolate-containing food"),
    (("spoon", "fork", "ladle", "skimmer", "cake-server", "kitchen"), "Kitchen or serving utensil"),
    (("milk", "cheese", "butter", "cream", "yoghurt", "dairy"), "Dairy product or dairy-based preparation"),
    (("meat", "sausage", "offal", "poultry"), "Meat or meat-based preparation"),
    (("rubber", "vulcanised", "vulcanized", "elastomer", "latex"), "Rubber article or rubber material"),
    (("fish", "crustacean", "mollusc", "aquatic invertebrate"), "Fish, shellfish, or aquatic product"),
    (("fruit", "vegetable", "nut", "peanut"), "Fruit, vegetable, nut, or plant preparation"),
    (("plastic", "polyethylene", "polyamide", "polymer"), "Plastic or polymer article/material"),
    (("iron", "steel", "aluminium", "metal", "copper"), "Metal article or metal material"),
    (("cotton", "textile", "woven", "knitted", "garment", "clothing"), "Textile, clothing, or fabric article"),
    (("paper", "cardboard", "pulp"), "Paper, cardboard, or pulp product"),
    (("wood", "timber"), "Wood or wooden article"),
    (("glass",), "Glass article/material"),
    (("ceramic", "porcelain"), "Ceramic or porcelain article/material"),
    (("medicament", "pharmaceutical", "medicine"), "Medicinal or pharmaceutical product"),
    (("electrical", "electronic", "battery", "motor"), "Electrical or electronic equipment"),
    (("vehicle", "motor car", "tractor", "trailer"), "Vehicle, vehicle part, or transport equipment"),
]


def _joined_candidate_text(candidate: dict, hydration: dict) -> str:
    # Keep this candidate-specific. Broad chapter ATAR/notes evidence can be useful
    # provenance, but it is often shared across many CCs and can contaminate labels.
    parts = [
        candidate.get("description"),
        (hydration.get("commodity") or {}).get("description"),
    ]
    source_texts = hydration.get("source_texts") or {}
    parts.extend([source_texts.get("self_text"), source_texts.get("search_text"), source_texts.get("composite_text")])
    goods_labels = source_texts.get("goods_labels") if isinstance(source_texts.get("goods_labels"), dict) else {}
    parts.extend([goods_labels.get("description"), goods_labels.get("original_description")])
    for key in ("synonyms", "colloquial_terms", "known_brands"):
        parts.extend(goods_labels.get(key) or [])
    parts.extend(source_texts.get("search_references") or [])
    parts.extend(source_texts.get("ancestor_path") or [])
    for facet in hydration.get("facets") or []:
        parts.extend([facet.get("facet_label"), facet.get("facet_value"), facet.get("evidence")])
    return _normal_text(" ".join(str(part or "") for part in parts))


def _product_family_label(text: str) -> str | None:
    for needles, label in _PRODUCT_FAMILY_RULES:
        if any(needle in text for needle in needles):
            return label
    return None


def _description_bucket(candidate: dict, hydration: dict) -> str:
    desc = _clean_label(
        candidate.get("description")
        or (hydration.get("commodity") or {}).get("description")
        or "",
        100,
    )
    for marker in ("; ", " (", " excl.", " other than ", " whether "):
        idx = desc.lower().find(marker)
        if idx > 25:
            desc = desc[:idx].rstrip(" ,.;")
            break
    return _clean_label(desc, 90)


def _add_signal(signals: list[dict], key: str, value: object, label: object, source: str, priority: int) -> None:
    clean_label = _clean_label(label, 95)
    norm_value = _normal_key(value or clean_label)
    if not clean_label or len(norm_value) < 3:
        return
    signals.append({
        "key": key,
        "value": norm_value,
        "label": clean_label,
        "source": source,
        "priority": priority,
    })


def _derive_candidate_signals(candidate: dict, hydration: dict) -> list[dict]:
    signals: list[dict] = []
    code = _candidate_code(candidate)
    digits = "".join(ch for ch in code if ch.isdigit())
    text = _joined_candidate_text(candidate, hydration)
    family = _product_family_label(text)
    if digits.startswith("40") and any(token in text for token in ("rubber", "vulcanised", "vulcanized", "elastomer", "latex")):
        family = "Rubber article or rubber material"
    if family:
        _add_signal(signals, "product_family", family, family, "text_family", 1)
    if len(digits) >= 4:
        heading_label = family or _description_bucket(candidate, hydration) or f"Heading {digits[:4]}"
        _add_signal(signals, "heading_area", digits[:4], heading_label, "code_heading", 2)
    for facet in hydration.get("facets") or []:
        value = facet.get("facet_value")
        if not value:
            continue
        key = str(facet.get("facet_key") or "facet")
        label = facet.get("facet_label") or key
        _add_signal(signals, f"facet:{key}", value, value, f"facet:{label}", _facet_priority(key))
    if any(token in text for token in ("sucrose", "sugar", "glucose", "isoglucose")):
        _add_signal(signals, "composition_detail", "sugar_sucrose", "Sugar/sucrose content matters", "description_text", 3)
    if any(token in text for token in ("cocoa", "chocolate")):
        _add_signal(signals, "composition_detail", "cocoa_chocolate", "Contains or is coated with cocoa/chocolate", "description_text", 3)
    if any(token in text for token in ("flour", "starch", "cereal", "wheat", "flakes", "bulgur")):
        _add_signal(signals, "composition_detail", "cereal_flour_starch", "Based on cereals, flour, starch, or wheat", "description_text", 3)
    if any(token in text for token in ("milk", "cheese", "egg", "butter", "cream")):
        _add_signal(signals, "composition_detail", "dairy_or_egg", "Contains dairy and/or egg ingredients", "description_text", 3)
    if any(token in text for token in ("peanut", "nut", "sesame")):
        _add_signal(signals, "composition_detail", "nuts_or_seeds", "Contains nuts or seeds", "description_text", 3)
    if any(token in text for token in ("bar", "bars", "cake", "cakes", "moulded", "molded", "pieces", "balls", "flakes")):
        _add_signal(signals, "presentation_form", "formed_pieces", "Bars, cakes, moulded pieces, balls, or flakes", "description_text", 4)
    if any(token in text for token in ("pack", "packing", "packaged", "net content", "retail sale", "bag", "boxes")):
        _add_signal(signals, "presentation_form", "packaged", "Packaged or in immediate packings", "description_text", 4)
    bucket = _description_bucket(candidate, hydration)
    if bucket:
        _add_signal(signals, "description_bucket", bucket, bucket, "description", 8)
    return signals



def _fact_sheet_label(labels: list[dict], kind: str, key: str, value: object, label: object, source: str, confidence: float = 1.0) -> None:
    clean_label = _clean_label(label or value, 140)
    norm_value = _normal_key(value or clean_label)
    if not clean_label or len(norm_value) < 3:
        return
    row = {
        "kind": kind,
        "key": key,
        "value": norm_value,
        "label": clean_label,
        "source": source,
        "confidence": max(0.0, min(float(confidence), 1.0)),
        "deterministic": True,
    }
    if row not in labels:
        labels.append(row)


def _deterministic_fact_sheet(candidate: dict, hydration: dict) -> dict:
    code = _candidate_code(candidate) or str(hydration.get("commodity_code") or "")
    flat = "".join(ch for ch in code if ch.isdigit()).ljust(10, "0")[:10]
    commodity = hydration.get("commodity") or {}
    labels: list[dict] = []

    candidate_desc = candidate.get("description")
    commodity_desc = commodity.get("description")
    source_texts = hydration.get("source_texts") or {}
    _fact_sheet_label(labels, "source_label", "candidate_description", candidate_desc, candidate_desc, "retrieval_candidate", 1.0)
    _fact_sheet_label(labels, "source_label", "commodity_description", commodity_desc, commodity_desc, "kg.commodity_view", 1.0)
    _fact_sheet_label(labels, "source_text", "self_text", source_texts.get("self_text"), source_texts.get("self_text"), "uk.goods_nomenclature_self_texts", 1.0)
    _fact_sheet_label(labels, "source_text", "search_text", source_texts.get("search_text"), source_texts.get("search_text"), "uk.goods_nomenclature_self_texts.search_text", 1.0)
    _fact_sheet_label(labels, "source_text", "composite_text", source_texts.get("composite_text"), source_texts.get("composite_text"), "kg.composite_search_text", 1.0)
    goods_labels = source_texts.get("goods_labels") if isinstance(source_texts.get("goods_labels"), dict) else {}
    _fact_sheet_label(labels, "source_label", "label_description", goods_labels.get("description"), goods_labels.get("description"), "uk.goods_nomenclature_labels.description", 0.9)
    _fact_sheet_label(labels, "source_label", "label_original_description", goods_labels.get("original_description"), goods_labels.get("original_description"), "uk.goods_nomenclature_labels.original_description", 1.0)
    for key in ("synonyms", "colloquial_terms", "known_brands"):
        for item in goods_labels.get(key) or []:
            _fact_sheet_label(labels, "source_label", key, item, item, f"uk.goods_nomenclature_labels.{key}", 0.85)
    for item in source_texts.get("search_references") or []:
        _fact_sheet_label(labels, "source_label", "search_reference", item, item, "uk.search_references", 0.8)
    for item in source_texts.get("ancestor_path") or []:
        _fact_sheet_label(labels, "ancestor_path", "ancestor_label", item, item, "uk.goods_nomenclature_self_texts.input_context", 1.0)
    _fact_sheet_label(labels, "regulatory_context", "measure_retrieval_doc", source_texts.get("measure_context"), source_texts.get("measure_context"), "kg.measure_retrieval_docs", 0.7)
    for note in source_texts.get("direct_footnotes") or []:
        if not isinstance(note, dict):
            continue
        label = f"{note.get('type') or ''}{note.get('id') or ''}: {note.get('description') or ''}".strip(": ")
        _fact_sheet_label(labels, "regulatory_context", "direct_footnote", label, label, "uk.footnote_association_goods_nomenclatures", 0.8)
    for fact in source_texts.get("llm_facts") or []:
        if not isinstance(fact, dict):
            continue
        label = _clean_label(fact.get("label") or fact.get("value"), 140)
        value = _clean_label(fact.get("value") or label, 140)
        key = str(fact.get("key") or "").strip()
        evidence = _clean_label(fact.get("evidence"), 260)
        if not key or not value or not label or not evidence:
            continue
        try:
            confidence = max(0.0, min(float(fact.get("confidence") or 0.0), 1.0))
        except Exception:
            confidence = 0.0
        try:
            qna_usefulness = max(0.0, min(float(fact.get("qna_usefulness") or 0.0), 1.0))
        except Exception:
            qna_usefulness = 0.0
        if confidence < 0.70:
            continue
        row = {
            "kind": "llm_enriched_fact",
            "key": key,
            "value": value,
            "label": label,
            "scope": str(fact.get("scope") or "").strip().lower(),
            "source": fact.get("source") or "kg.commodity_llm_facts",
            "evidence": evidence,
            "confidence": confidence,
            "qna_usefulness": qna_usefulness,
            "question_hint": fact.get("question_hint") or "",
            "model": fact.get("model"),
            "prompt_version": fact.get("prompt_version"),
            "run_id": fact.get("run_id"),
            "provenance": "kg.commodity_llm_facts",
            "deterministic": False,
        }
        if row not in labels:
            labels.append(row)
    if flat.strip("0"):
        _fact_sheet_label(labels, "code_label", "heading", flat[:4], f"Heading {flat[:4]}", "commodity_code", 1.0)
        _fact_sheet_label(labels, "code_label", "chapter", flat[:2], f"Chapter {flat[:2]}", "commodity_code", 1.0)

    for facet in hydration.get("facets") or []:
        value = facet.get("facet_value")
        if not value:
            continue
        key = str(facet.get("facet_key") or "facet")
        label = facet.get("facet_label") or key
        _fact_sheet_label(
            labels,
            "source_facet",
            key,
            value,
            value,
            str(facet.get("source") or "kg.commodity_facets"),
            float(facet.get("confidence") or 1.0),
        )

    signals = _derive_candidate_signals(candidate, hydration)
    for signal in signals:
        _fact_sheet_label(
            labels,
            "derived_signal",
            str(signal.get("key") or "signal"),
            signal.get("value") or signal.get("label"),
            signal.get("label") or signal.get("value"),
            str(signal.get("source") or "deterministic_rules"),
            1.0,
        )

    source_counts = {
        "source_labels": sum(1 for row in labels if row.get("kind") == "source_label"),
        "source_texts": sum(1 for row in labels if row.get("kind") == "source_text"),
        "source_facets": sum(1 for row in labels if row.get("kind") == "source_facet"),
        "ancestor_labels": sum(1 for row in labels if row.get("kind") == "ancestor_path"),
        "regulatory_context": sum(1 for row in labels if row.get("kind") == "regulatory_context"),
        "llm_enriched_facts": sum(1 for row in labels if row.get("kind") == "llm_enriched_fact"),
        "derived_signals": sum(1 for row in labels if row.get("kind") == "derived_signal"),
        "kg_evidence": len(hydration.get("evidence") or []),
    }
    return {
        "version": _FACT_SHEET_VERSION,
        "commodity_code": flat or code,
        "code_dotted": _code_dotted(flat or code),
        "labels": labels,
        "signals": signals,
        "counts": {
            "labels": len(labels),
            "signals": len(signals),
            **source_counts,
        },
        "construction": {
            "mode": "deterministic",
            "source_material": [
                "retrieval_candidate.description",
                "kg.commodity_view.commodity",
                "uk.goods_nomenclature_self_texts",
                "uk.goods_nomenclature_self_texts.search_text",
                "uk.goods_nomenclature_self_texts.input_context",
                "uk.goods_nomenclature_labels",
                "uk.search_references",
                "kg.measure_retrieval_docs",
                "uk.footnote_association_goods_nomenclatures",
                "kg.commodity_llm_facts",
                "kg.composite_search_text",
                "kg.commodity_view.facets",
                "commodity_code_hierarchy",
            ],
            "excluded_from_automatic_labels": [
                "broad chapter ATAR bodies",
                "shared chapter notes",
                "raw measure duty rows",
                "raw measure condition rows",
                "raw quota event tables",
                "unlinked KG edges",
            ],
        },
        "neurosymbolic": {
            "llm_enrichment_status": "not_run",
            "llm_may_propose_labels": True,
            "llm_may_phrase_questions": True,
            "llm_may_mutate_candidate_state": False,
            "state_update_authority": "deterministic_option_code_mapping",
            "required_validation": [
                "schema_valid",
                "candidate_code_scoped",
                "source_provenance_present",
                "answer_options_map_to_candidate_codes",
            ],
        },
    }


def _ensure_fact_sheet(candidate: dict, hydration: dict) -> tuple[dict, bool]:
    fact_sheet = hydration.get("fact_sheet") if isinstance(hydration.get("fact_sheet"), dict) else None
    if fact_sheet and fact_sheet.get("version") == _FACT_SHEET_VERSION and isinstance(fact_sheet.get("signals"), list):
        return hydration, False
    upgraded = dict(hydration)
    upgraded["fact_sheet"] = _deterministic_fact_sheet(candidate, upgraded)
    return upgraded, True


def _candidate_signals(candidate: dict, hydration: dict) -> list[dict]:
    fact_sheet = hydration.get("fact_sheet") if isinstance(hydration.get("fact_sheet"), dict) else None
    signals = fact_sheet.get("signals") if fact_sheet else None
    base = list(signals) if isinstance(signals, list) and signals else _derive_candidate_signals(candidate, hydration)
    if fact_sheet:
        base.extend(_llm_fact_signals(fact_sheet))
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for signal in base:
        key = str(signal.get("key") or "")
        value = str(signal.get("value") or "")
        if not key or not value or (key, value) in seen:
            continue
        seen.add((key, value))
        deduped.append(signal)
    return deduped


_LLM_QA_FACT_KEYS = {
    "product_family",
    "common_name",
    "species_or_variety",
    "material",
    "composition",
    "form_or_presentation",
    "processing_state",
    "intended_use",
    "packaging",
    "inclusion",
    "exclusion",
    "threshold_condition",
}


_LLM_FACT_PRIORITIES = {
    "product_family": 2,
    "common_name": 3,
    "species_or_variety": 3,
    "material": 4,
    "composition": 4,
    "processing_state": 4,
    "form_or_presentation": 5,
    "packaging": 5,
    "intended_use": 5,
    "inclusion": 6,
    "exclusion": 6,
    "threshold_condition": 6,
}


def _normal_llm_fact_key(key: object) -> str:
    clean = _normal_key(key)
    aliases = {
        "formorpresentation": "form_or_presentation",
        "presentation": "form_or_presentation",
        "form": "form_or_presentation",
        "processing": "processing_state",
        "processingstate": "processing_state",
        "species": "species_or_variety",
        "variety": "species_or_variety",
        "speciesorvariety": "species_or_variety",
        "use": "intended_use",
        "function": "intended_use",
        "commonname": "common_name",
        "productfamily": "product_family",
        "threshold": "threshold_condition",
        "condition": "threshold_condition",
    }
    return aliases.get(clean, str(key or "").strip().lower())


def _bucket_llm_fact_value(key: str, value: object, label: object) -> tuple[str, str] | None:
    raw = _clean_label(value or label, 120)
    if not raw:
        return None
    text = _normal_text(raw)
    bucket: str | None = None
    display = raw
    if key == "product_family":
        bucket = _product_family_label(text)
    elif key in {"material", "composition"}:
        material_buckets = [
            (("non alloy steel", "nonalloy steel", "iron or steel", "steel", "iron"), "Iron or steel"),
            (("aluminium", "aluminum"), "Aluminium"),
            (("copper",), "Copper"),
            (("plastic", "polymer", "polyethylene", "polypropylene", "polyamide"), "Plastic or polymer"),
            (("cotton",), "Cotton"),
            (("wool",), "Wool"),
            (("leather",), "Leather"),
            (("wood", "timber"), "Wood"),
            (("glass",), "Glass"),
            (("ceramic", "porcelain"), "Ceramic or porcelain"),
            (("sugar", "sucrose", "glucose", "isoglucose"), "Sugar/sucrose/glucose"),
            (("cocoa", "chocolate"), "Cocoa or chocolate"),
            (("milk", "dairy", "cheese", "butter", "cream"), "Dairy"),
        ]
        for needles, name in material_buckets:
            if any(needle in text for needle in needles):
                bucket = name
                break
    elif key == "processing_state":
        state_buckets = [
            (("fresh",), "Fresh"),
            (("chilled",), "Chilled"),
            (("frozen",), "Frozen"),
            (("dried", "dry"), "Dried"),
            (("smoked",), "Smoked"),
            (("prepared", "preserved"), "Prepared or preserved"),
            (("raw", "unprocessed"), "Raw or unprocessed"),
        ]
        for needles, name in state_buckets:
            if any(needle in text for needle in needles):
                bucket = name
                break
    elif key in {"form_or_presentation", "packaging"}:
        form_buckets = [
            (("powder",), "Powder"),
            (("liquid", "solution"), "Liquid or solution"),
            (("sheet", "plate", "strip"), "Sheet, plate, or strip"),
            (("roll", "rolled"), "Rolls"),
            (("bar", "rod"), "Bars or rods"),
            (("straight length",), "Straight lengths"),
            (("retail", "pack", "package", "bag", "box"), "Retail packed or packaged"),
        ]
        for needles, name in form_buckets:
            if any(needle in text for needle in needles):
                bucket = name
                break
    if bucket:
        display = bucket
        return _normal_key(bucket), display
    return _normal_key(raw), raw


def _llm_fact_signals(fact_sheet: dict) -> list[dict]:
    out: list[dict] = []
    for row in fact_sheet.get("labels") or []:
        if not isinstance(row, dict) or row.get("kind") != "llm_enriched_fact":
            continue
        key = _normal_llm_fact_key(row.get("key"))
        if key not in _LLM_QA_FACT_KEYS:
            continue
        try:
            confidence = float(row.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        try:
            qna_usefulness = float(row.get("qna_usefulness") or 0.0)
        except Exception:
            qna_usefulness = 0.0
        if confidence < 0.70 or qna_usefulness < 0.65:
            continue
        evidence = _clean_label(row.get("evidence"), 220)
        if not evidence or "short supporting phrase" in evidence.lower() or "source material" in evidence.lower():
            continue
        bucket = _bucket_llm_fact_value(key, row.get("value"), row.get("label"))
        if not bucket:
            continue
        value, label = bucket
        if len(value) < 3:
            continue
        out.append({
            "key": f"llm_fact:{key}",
            "value": value,
            "label": label,
            "source": f"llm_enriched_fact:{row.get('model') or 'llm'}",
            "priority": _LLM_FACT_PRIORITIES.get(key, 7),
            "confidence": confidence,
            "qna_usefulness": qna_usefulness,
        })
    return out



_OPTION_MAP_STOPWORDS = {
    "with", "from", "that", "this", "these", "those", "product", "products",
    "goods", "good", "type", "kind", "based", "containing", "contains",
    "similar", "other", "preparation", "preparations", "article", "articles",
}


def _mapping_terms(value: object) -> set[str]:
    return {
        token
        for token in _normal_text(value).replace("/", " ").split()
        if len(token) >= 4 and token not in _OPTION_MAP_STOPWORDS
    }


def _candidate_mapping_text(candidate: dict, hydration: dict) -> str:
    hydration, _ = _ensure_fact_sheet(candidate, hydration)
    fact_sheet = hydration.get("fact_sheet") or {}
    parts = [
        candidate.get("description"),
        (hydration.get("commodity") or {}).get("description"),
    ]
    for row in fact_sheet.get("labels") or []:
        parts.extend([row.get("label"), row.get("key"), row.get("source")])
    for row in fact_sheet.get("signals") or []:
        parts.extend([row.get("label"), row.get("key"), row.get("source")])
    return _normal_text(" ".join(str(part or "") for part in parts))


def _map_llm_options_to_candidate_codes(options: list[str], active: list[dict], records_by_code: dict[str, dict]) -> list[dict]:
    active_codes = [_candidate_code(candidate) for candidate in active]
    candidate_text: dict[str, str] = {}
    candidate_terms: dict[str, set[str]] = {}
    for candidate in active:
        code = _candidate_code(candidate)
        hydration = (records_by_code.get(code) or {}).get("hydration") or {}
        text = _candidate_mapping_text(candidate, hydration)
        candidate_text[code] = text
        candidate_terms[code] = _mapping_terms(text)

    mapped: list[dict] = []
    seen_labels: set[str] = set()
    for raw in options:
        label = _clean_label(raw, 95)
        if not label or "none of these" in label.lower() or label.lower() in {"other", "something else"}:
            continue
        label_norm = _normal_key(label)
        option_terms = _mapping_terms(label)
        if not option_terms and len(label_norm) < 8:
            continue
        codes: list[str] = []
        for code in active_codes:
            text = candidate_text.get(code) or ""
            direct = bool(label_norm and len(label_norm) >= 8 and label_norm in _normal_key(text))
            overlap = len(option_terms & candidate_terms.get(code, set()))
            threshold = max(1, min(3, int(len(option_terms) * 0.45 + 0.999)))
            if direct or overlap >= threshold:
                codes.append(code)
        if not codes or len(codes) >= len(active_codes):
            continue
        dedupe_key = _normal_key(label)
        if dedupe_key in seen_labels:
            continue
        seen_labels.add(dedupe_key)
        mapped.append({
            "value": dedupe_key,
            "label": label,
            "candidate_count": len(codes),
            "codes": codes[:50],
            "mapping": {
                "source": "symbolic_fact_sheet_option_mapping",
                "deterministic": True,
            },
        })
        if len(mapped) >= 6:
            break

    if mapped:
        mapped.append({
            "value": "__none__",
            "label": _NONE_OPTION,
            "candidate_count": len(active_codes),
            "codes": active_codes[:50],
            "mapping": {
                "source": "none_keeps_broader_shortlist",
                "deterministic": True,
            },
        })
    return mapped[:7]


def _question_for_signal(signal_key: str, label: str) -> str:
    if signal_key.startswith("llm_fact:"):
        fact_key = signal_key.split(":", 1)[1]
        if fact_key == "product_family":
            return "Which broad product type best matches the goods?"
        if fact_key == "common_name":
            return "Which product name is closest to the goods?"
        if fact_key == "species_or_variety":
            return "Which species, variety, or type best matches?"
        if fact_key in {"material", "composition"}:
            return "What are the goods made of or composed of?"
        if fact_key == "processing_state":
            return "What processing state are the goods in?"
        if fact_key in {"form_or_presentation", "packaging"}:
            return "How are the goods presented or packed?"
        if fact_key == "intended_use":
            return "What are the goods intended to be used for?"
        if fact_key in {"inclusion", "exclusion", "threshold_condition"}:
            return "Which classification condition best matches the goods?"
    if signal_key == "product_family":
        return "Which broad product type best matches the goods?"
    if signal_key == "heading_area":
        return "Which broad product area best matches the goods?"
    if signal_key == "composition_detail":
        return "Which composition detail best matches the goods?"
    if signal_key == "presentation_form":
        return "How are the goods presented or packed?"
    if signal_key == "description_bucket":
        return "Which description is closest to the goods?"
    if signal_key.startswith("facet:"):
        facet_key = signal_key.split(":", 1)[1]
        return _question_for_facet(facet_key, label)
    clean = _clean_label(label or signal_key, 80).lower()
    return f"Which {clean} best describes the goods?"

def _candidate_matches_answer(candidate: dict, record: dict | None, turn: dict) -> bool:
    answer = str(turn.get("answer") or "").strip()
    if not answer or "none of these" in answer.lower():
        return True
    code = _candidate_code(candidate)
    code_norm = "".join(ch for ch in code if ch.isdigit()).ljust(10, "0")[:10]
    answer_value = str(turn.get("answer_value") or "").strip().lower()
    answer_norm = _normal_key(answer)

    for option in turn.get("options_meta") or []:
        label_norm = _normal_key(option.get("label"))
        value_norm = _normal_key(option.get("value"))
        selected = False
        if answer_norm and answer_norm in (label_norm, value_norm):
            selected = True
        if answer_value and answer_value in (str(option.get("value") or "").lower(), str(option.get("label") or "").lower()):
            selected = True
        if selected:
            option_codes = {
                "".join(ch for ch in str(raw or "") if ch.isdigit()).ljust(10, "0")[:10]
                for raw in option.get("codes") or []
            }
            return code_norm in option_codes

    if answer_value and answer_value == code.lower():
        return True
    hydration = (record or {}).get("hydration") or {}
    view_facets = hydration.get("facets") or []
    facet_key = str(turn.get("facet_key") or "").strip()
    signal_key = str(turn.get("signal_key") or "").strip()
    if signal_key and signal_key != "description_bucket":
        for signal in _candidate_signals(candidate, hydration):
            if signal.get("key") != signal_key:
                continue
            if answer_norm and answer_norm in (_normal_key(signal.get("label")), _normal_key(signal.get("value"))):
                return True
    for facet in view_facets:
        if facet_key and facet.get("facet_key") != facet_key:
            continue
        value = str(facet.get("facet_value") or "").strip().lower()
        label = _clean_label(value)
        if answer_value and value == answer_value:
            return True
        value_norm = _normal_key(value)
        label_norm = _normal_key(label)
        if answer_norm and (answer_norm == value_norm or answer_norm == label_norm):
            return True
    option = _normal_key(_qa_option(candidate, {"facets": view_facets, "description": hydration.get("commodity", {}).get("description")}))
    desc = _normal_key(candidate.get("description"))
    return bool(answer_norm and (answer_norm == option or answer_norm == desc or answer_norm in desc or desc in answer_norm))

def _apply_qa_history(candidates: list[dict], records_by_code: dict[str, dict], qa_history: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    active = list(candidates)
    ruled_out: dict[str, dict] = {}
    trace: list[dict] = []
    for turn in qa_history:
        answer = str(turn.get("answer") or "").strip()
        if not answer or "none of these" in answer.lower():
            trace.append({"answer": answer, "action": "kept_all", "matched_count": len(active)})
            continue
        matched = [c for c in active if _candidate_matches_answer(c, records_by_code.get(_candidate_code(c)), turn)]
        if not matched:
            trace.append({"answer": answer, "action": "kept_all_no_match", "matched_count": 0})
            continue
        matched_codes = {_candidate_code(c) for c in matched}
        removed = [c for c in active if _candidate_code(c) not in matched_codes]
        for candidate in removed:
            code = _candidate_code(candidate)
            ruled_out[code] = {**candidate, "reason": f"Answer did not match: {answer}"}
        active = matched
        trace.append({
            "answer": answer,
            "facet_key": turn.get("facet_key"),
            "matched_count": len(matched),
            "ruled_out_count": len(removed),
            "action": "filtered",
        })
    return active, list(ruled_out.values()), trace


def _pick_facet_question(active: list[dict], records_by_code: dict[str, dict], payload: dict, use_llm_wording: bool) -> dict | None:
    import math
    from collections import Counter, defaultdict

    active_codes = [_candidate_code(c) for c in active]
    key_to_values: dict[str, list[str]] = defaultdict(list)
    key_to_codes: dict[str, set[str]] = defaultdict(set)
    key_value_to_codes: dict[tuple[str, str], set[str]] = defaultdict(set)
    key_value_labels: dict[tuple[str, str], str] = {}
    key_labels: dict[str, str] = {}
    key_priorities: dict[str, int] = {}
    key_sources: dict[str, set[str]] = defaultdict(set)
    hydrated_count = 0

    for code in active_codes:
        record = records_by_code.get(code)
        if not record:
            continue
        hydration = (record.get("hydration") or {})
        hydrated_count += 1
        seen: set[tuple[str, str]] = set()
        for signal in _candidate_signals(record.get("candidate") or {}, hydration):
            key = str(signal.get("key") or "")
            value = str(signal.get("value") or "")
            label = str(signal.get("label") or value)
            if not key or not value:
                continue
            pair = (key, value)
            if pair in seen:
                continue
            seen.add(pair)
            key_to_values[key].append(value)
            key_to_codes[key].add(code)
            key_value_to_codes[pair].add(code)
            key_value_labels[pair] = label
            key_labels[key] = key.replace("facet:", "").replace("_", " ")
            key_priorities[key] = min(key_priorities.get(key, 99), int(signal.get("priority") or 9))
            key_sources[key].add(str(signal.get("source") or "hydrated_payload"))

    denom = max(1, hydrated_count)
    candidates = []
    for key, values in key_to_values.items():
        distinct = sorted(set(values))
        coverage = len(key_to_codes[key]) / denom
        min_coverage = 0.10 if (key.startswith("facet:") or key.startswith("llm_fact:")) else 0.20
        if coverage < min_coverage or len(distinct) < 2:
            continue
        counts = Counter({value: len(key_value_to_codes[(key, value)]) for value in distinct})
        total = sum(counts.values())
        if total <= 0:
            continue
        entropy = -sum((count / total) * math.log2(count / total) for count in counts.values())
        largest_bucket = max(counts.values()) / total
        if largest_bucket >= 0.98:
            continue
        option_count_penalty = max(0, len(distinct) - 6) * 0.25
        candidates.append({
            "signal_key": key,
            "values": distinct,
            "value_counts": dict(counts),
            "coverage": coverage,
            "entropy": entropy,
            "priority": key_priorities.get(key, 9),
            "option_count_penalty": option_count_penalty,
            "sources": sorted(key_sources[key]),
        })

    if not candidates:
        return None
    non_description = [row for row in candidates if row.get("signal_key") != "description_bucket"]
    if non_description:
        candidates = non_description

    candidates.sort(key=lambda row: (
        row["priority"],
        row["option_count_penalty"],
        -row["coverage"],
        -row["entropy"],
        len(row["values"]),
    ))
    winner = candidates[0]
    signal_key = winner["signal_key"]
    label = key_labels.get(signal_key, signal_key)
    ordered_values = sorted(
        winner["values"],
        key=lambda value: (-len(key_value_to_codes[(signal_key, value)]), key_value_labels.get((signal_key, value), value)),
    )

    options: list[str] = []
    options_meta: list[dict] = []
    for value in ordered_values[:6]:
        option_label = _clean_label(key_value_labels.get((signal_key, value), value), 95)
        codes = sorted(key_value_to_codes[(signal_key, value)], key=lambda c: active_codes.index(c) if c in active_codes else 999)
        if not option_label or option_label in options:
            continue
        options.append(option_label)
        options_meta.append({
            "value": value,
            "label": option_label,
            "candidate_count": len(codes),
            "codes": codes[:50],
        })
    if len(ordered_values) > 6:
        shown = {meta["value"] for meta in options_meta}
        other_codes = sorted(
            {code for value in ordered_values if value not in shown for code in key_value_to_codes[(signal_key, value)]},
            key=lambda c: active_codes.index(c) if c in active_codes else 999,
        )
        if other_codes:
            options.append("Something else in the shortlist")
            options_meta.append({
                "value": "__other_signal__",
                "label": "Something else in the shortlist",
                "candidate_count": len(other_codes),
                "codes": other_codes[:50],
            })
    if _NONE_OPTION not in options:
        options.append(_NONE_OPTION)
        options_meta.append({"value": "__none__", "label": _NONE_OPTION, "candidate_count": len(active), "codes": active_codes[:50]})

    question = _question_for_signal(signal_key, label)
    source = "fact_sheet_facet_rules"
    provider_used = False
    model = None
    if use_llm_wording:
        question, source, provider_used, model = _rewrite_hydrated_question(question, options, payload, source="facet_rules_llm_wording")
    return {
        "question": question,
        "options": options[:7],
        "source": source,
        "mode": "facet_rules_llm_wording" if provider_used else "facet_rules",
        "requested_mode": _question_mode(payload),
        "provider_used": provider_used,
        "model": model,
        "signal_key": signal_key,
        "facet_key": signal_key.split(":", 1)[1] if signal_key.startswith("facet:") else None,
        "facet_label": label,
        "entropy": winner["entropy"],
        "options_meta": options_meta[:7],
        "debug": {
            "coverage": winner["coverage"],
            "candidate_signal_keys": len(key_to_values),
            "sources": winner["sources"],
            "facet_assembly": {
                "source": "fact_sheet.signals + validated llm_enriched_fact labels",
                "deterministic": True,
                "active_candidate_count": len(active_codes),
                "state_updates": "option_code_mapping_only",
            },
            "top_signal_keys": [
                {
                    "signal_key": row["signal_key"],
                    "coverage": row["coverage"],
                    "entropy": row["entropy"],
                    "priority": row["priority"],
                    "values": len(row["values"]),
                    "sources": row["sources"],
                }
                for row in candidates[:5]
            ],
        },
    }

def _fallback_question(active: list[dict], records_by_code: dict[str, dict], payload: dict) -> dict:
    options = []
    options_meta = []
    for candidate in active[:6]:
        code = _candidate_code(candidate)
        view = (records_by_code.get(code) or {}).get("hydration") or {}
        option = _qa_option(candidate, {"facets": view.get("facets") or [], "description": candidate.get("description")})
        if option and option not in options:
            options.append(option)
            options_meta.append({"value": code, "label": option, "candidate_count": 1, "codes": [code]})
    if _NONE_OPTION not in options:
        options.append(_NONE_OPTION)
        options_meta.append({"value": "__none__", "label": _NONE_OPTION, "candidate_count": len(active), "codes": [_candidate_code(c) for c in active[:20]]})
    question = "In plain language, which bucket is closest to your goods?"
    return {
        "question": question,
        "options": options[:7],
        "source": "facet_rules_fallback",
        "mode": "facet_rules",
        "requested_mode": _question_mode(payload),
        "provider_used": False,
        "options_meta": options_meta[:7],
        "fallback_reason": "No discriminating hydrated signal met the coverage/distinct-value thresholds.",
    }


def _rewrite_hydrated_question(question: str, options: list[str], payload: dict, source: str = "facet_rules_llm_wording") -> tuple[str, str, bool, str | None]:
    if payload.get("allow_spend") is not True:
        return question, "facet_rules", False, None
    cfg = load_config()
    api_key = cfg.api_keys.openai
    if not api_key:
        return question, "facet_rules", False, None
    model = str((payload.get("config") or {}).get("question_wording_model") or "gpt-5-nano")
    try:
        from openai import OpenAI
        resp = OpenAI(api_key=api_key, timeout=12).chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Rewrite a customs classification question for a real trader. "
                        "Keep it short, plain-English, and answerable from commercial/product knowledge. "
                        "Do not mention commodity codes, tariff headings, GIRs, duty, VAT, or import dates. "
                        "Do not add, remove, or imply answer options. Return JSON only: {\"question\":\"...\"}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"draft_question": question, "fixed_options": options[:8]}),
                },
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        rewritten = " ".join(str(data.get("question") or "").split())
        if 12 <= len(rewritten) <= 140 and "commodity code" not in rewritten.lower():
            return (rewritten if rewritten.endswith("?") else f"{rewritten}?"), source, True, model
    except Exception as exc:
        print(f"[hydrated qa wording] {type(exc).__name__}: {exc}")
    return question, "facet_rules", False, None


def _llm_generated_question(active: list[dict], records_by_code: dict[str, dict], payload: dict, qa_history: list[dict]) -> dict | None:
    if payload.get("allow_spend") is not True:
        return None
    cfg = load_config()
    api_key = cfg.api_keys.openai
    if not api_key:
        return None
    model = str((payload.get("config") or {}).get("question_wording_model") or "gpt-5-nano")
    rows = []
    for candidate in active[:10]:
        code = _candidate_code(candidate)
        hydration = (records_by_code.get(code) or {}).get("hydration") or {}
        hydration, _ = _ensure_fact_sheet(candidate, hydration)
        fact_sheet = hydration.get("fact_sheet") or {}
        rows.append({
            "description": candidate.get("description") or hydration.get("commodity", {}).get("description") or "",
            "fact_sheet": {
                "labels": [
                    {k: row.get(k) for k in ("kind", "key", "label", "source", "confidence")}
                    for row in (fact_sheet.get("labels") or [])[:12]
                ],
                "signals": [
                    {k: row.get(k) for k in ("key", "label", "source", "priority")}
                    for row in (fact_sheet.get("signals") or [])[:12]
                ],
            },
        })
    try:
        from openai import OpenAI
        resp = OpenAI(api_key=api_key, timeout=12).chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Create one plain-English customs classification question and answer options for a trader. "
                        "Use only the supplied candidate descriptions and hydrated fact-sheet labels/signals. "
                        "Options must be short trader-facing product buckets, not copied tariff descriptions. "
                        "Rephrase in normal English, remove corrupted characters, and keep each option under 80 characters. "
                        "Do not mention commodity codes, tariff headings, GIRs, duty, VAT, origin, certificates, or import dates. "
                        "Return JSON only: {\"question\":\"...\",\"options\":[\"...\"]}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "prior_qa": qa_history,
                        "hydrated_candidates": rows,
                    }),
                },
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        question = _clean_label(data.get("question"), 160)
        if not question or len(question) > 160 or "commodity code" in question.lower():
            return None
        options = []
        for raw in data.get("options") or []:
            option = _clean_label(raw, 120)
            if option and "commodity code" not in option.lower() and option not in options:
                options.append(option)
            if len(options) >= 6:
                break
        if len(options) < 2:
            return None
        options_meta = _map_llm_options_to_candidate_codes(options, active, records_by_code)
        mapped_non_none = [row for row in options_meta if row.get("value") != "__none__"]
        if len(mapped_non_none) < 2:
            return None
        mapped_options = [row["label"] for row in options_meta]
        return {
            "question": question if question.endswith("?") else f"{question}?",
            "options": mapped_options,
            "source": "llm_generated_validated_fact_sheet",
            "mode": "llm_generated",
            "requested_mode": "llm_generated",
            "provider_used": True,
            "model": model,
            "options_meta": options_meta,
            "validation": {
                "llm_role": "question_and_option_proposal",
                "state_update_authority": "symbolic_fact_sheet_option_mapping",
                "accepted_only_if_options_map_to_candidate_codes": True,
            },
        }
    except Exception as exc:
        print(f"[hydrated qa llm generated] {type(exc).__name__}: {exc}")
    return None


@app.post("/api/hydration/candidates")
async def api_hydration_candidates(payload: dict):
    candidates = payload.get("candidates") or []
    if not candidates:
        raise HTTPException(400, "candidates are required")
    candidate_limit = max(1, min(int(payload.get("candidate_limit") or len(candidates)), 500))
    hydrate_limit_raw = int(payload.get("hydrate_limit") or 0)
    hydrate_limit = min(candidate_limit, len(candidates)) if hydrate_limit_raw <= 0 else min(hydrate_limit_raw, candidate_limit, len(candidates))
    limited_candidates = candidates[:candidate_limit]
    question_mode = _question_mode(payload)
    qa_history = payload.get("qa_history") if isinstance(payload.get("qa_history"), list) else []

    hydrated = []
    coverage_totals: dict[str, int] = {}
    records_by_code: dict[str, dict] = {}
    hydrate_codes = [_candidate_code(candidate) for candidate in limited_candidates[:hydrate_limit]]
    cached_by_code = await _load_hydration_cache(hydrate_codes)
    source_texts_by_code = await _load_source_texts(hydrate_codes)
    cache_hit_count = 0
    cache_write_count = 0
    for candidate in limited_candidates[:hydrate_limit]:
        code = _candidate_code(candidate)
        if not code:
            continue
        flat = "".join(ch for ch in code if ch.isdigit()).ljust(10, "0")[:10]
        cached = cached_by_code.get(flat)
        if cached:
            source_texts = source_texts_by_code.get(flat)
            if source_texts and cached.get("source_texts") != source_texts:
                cached = {**cached, "source_texts": source_texts}
            cached, upgraded = _ensure_fact_sheet(candidate, cached)
            if upgraded:
                await _write_hydration_cache(cached)
                cache_write_count += 1
            record = {"candidate": candidate, "hydration": cached}
            hydrated.append(record)
            records_by_code[code] = record
            records_by_code[flat] = record
            for kind, count in ((cached.get("coverage") or {}).get("counts_by_kind") or {}).items():
                coverage_totals[kind] = coverage_totals.get(kind, 0) + int(count)
            fact_counts = (cached.get("fact_sheet") or {}).get("counts") or {}
            coverage_totals["fact_sheet_label"] = coverage_totals.get("fact_sheet_label", 0) + int(fact_counts.get("labels") or 0)
            coverage_totals["fact_sheet_signal"] = coverage_totals.get("fact_sheet_signal", 0) + int(fact_counts.get("signals") or 0)
            cache_hit_count += 1
            continue

        view = await kg.commodity_view(code)
        evidence, counts = _build_evidence(view)
        for kind, count in counts.items():
            coverage_totals[kind] = coverage_totals.get(kind, 0) + int(count)
        record = {
            "candidate": candidate,
            "hydration": {
                "ok": True,
                "commodity_code": view.get("code") or flat,
                "code_dotted": _code_dotted(view.get("code") or flat),
                "commodity": {
                    "code": view.get("code") or flat,
                    "description": view.get("description") or candidate.get("description") or "",
                },
                "coverage": {"counts_by_kind": counts},
                "summary": {"mode": "deterministic", "counts_by_kind": counts},
                "evidence": evidence,
                "facets": view.get("facets") or [],
                "source_texts": source_texts_by_code.get(flat) or {},
                "cache": {"version": _HYDRATION_CACHE_VERSION, "source": "kg.commodity_view"},
            },
        }
        record["hydration"], _ = _ensure_fact_sheet(candidate, record["hydration"])
        fact_counts = (record["hydration"].get("fact_sheet") or {}).get("counts") or {}
        coverage_totals["fact_sheet_label"] = coverage_totals.get("fact_sheet_label", 0) + int(fact_counts.get("labels") or 0)
        coverage_totals["fact_sheet_signal"] = coverage_totals.get("fact_sheet_signal", 0) + int(fact_counts.get("signals") or 0)
        await _write_hydration_cache(record["hydration"])
        cache_write_count += 1
        hydrated.append(record)
        records_by_code[code] = record
        records_by_code[flat] = record

    try:
        from experiment_retrieval import query_difficulty_from_candidates, query_lexical_specificity
        lexical_specificity = payload.get("lexical_specificity") or query_lexical_specificity(str(payload.get("query") or ""))
        query_difficulty = payload.get("query_difficulty") or query_difficulty_from_candidates(str(payload.get("query") or ""), limited_candidates, k=candidate_limit)
    except Exception as exc:
        lexical_specificity = {"available": False, "label": "Lexical specificity", "score": None, "note": f"{type(exc).__name__}: {exc}"}
        query_difficulty = {"available": False, "label": "Query difficulty", "score": None, "note": f"{type(exc).__name__}: {exc}"}

    active, ruled_out, filter_trace = _apply_qa_history(limited_candidates, records_by_code, qa_history)
    try:
        from experiment_retrieval import query_difficulty_from_candidates
        active_query_difficulty = query_difficulty_from_candidates(str(payload.get("query") or ""), active, k=max(1, len(active)))
    except Exception as exc:
        active_query_difficulty = {"available": False, "label": "Active shortlist difficulty", "score": None, "note": f"{type(exc).__name__}: {exc}"}
    qa_state = {
        "qa_history": qa_history,
        "round": len(qa_history) + 1,
        "in_scope_count": len(active),
        "out_of_scope_count": len(ruled_out),
        "in_scope_codes": [_candidate_code(c) for c in active],
        "out_of_scope_codes": [_candidate_code(c) for c in ruled_out],
        "filter_trace": filter_trace,
    }

    question_hint = None
    if question_mode == "llm_generated":
        question_hint = _llm_generated_question(active, records_by_code, payload, qa_history)
    if question_hint is None:
        question_hint = _pick_facet_question(
            active,
            records_by_code,
            payload,
            use_llm_wording=(question_mode == "facet_rules_llm_wording"),
        )
    if question_hint is None:
        question_hint = _fallback_question(active, records_by_code, payload)

    return {
        "query": str(payload.get("query") or ""),
        "summarize": False,
        "question_mode": question_mode,
        "candidate_count": len(limited_candidates),
        "hydrate_limit": hydrate_limit,
        "cache_write": cache_write_count > 0,
        "cache_hit_count": cache_hit_count,
        "cache_write_count": cache_write_count,
        "cache_version": _HYDRATION_CACHE_VERSION,
        "retrieval_guardrail": "Hydrates already-retrieved candidate codes only and caches hydrated CC payloads in the KG schema.",
        "fact_sheet_version": _FACT_SHEET_VERSION,
        "determinism_contract": {
            "fact_sheet_construction": "deterministic_rules_plus_validated_kg_llm_facts; LLM enrichment may only propose validated labels",
            "runtime_facet_assembly": "deterministic_from_active_shortlist_fact_sheets",
            "candidate_state_updates": "deterministic_option_code_mapping_only",
            "llm_allowed_roles": ["question_wording", "offline_fact_label_proposals", "validated_question_proposals"],
        },
        "candidates": limited_candidates,
        "hydrated": hydrated,
        "coverage_totals": coverage_totals,
        "lexical_specificity": lexical_specificity,
        "query_difficulty": query_difficulty,
        "active_query_difficulty": active_query_difficulty,
        "question_hint": question_hint,
        "qa_state": qa_state,
    }


# --- Intercept-list complexity analysis ---


@app.get("/api/intercepts/terms")
def api_intercepts_list_terms():
    """List the HMRC intercept terms with their spreadsheet metadata."""
    import intercepts
    return intercepts.list_terms()


@app.post("/api/intercepts/analyze")
async def api_intercepts_analyze(payload: dict):
    """Run hybrid retrieval + complexity KPIs for given term indices.

    Request: {
        "indices": int[] | null   # null = all terms,
        "k": int = 30,
        "over_fetch": int = 200,
        "weights": object | null,
    }
    Response: {
        "k": ..., "rows": [...], "details": { term: {kpis, candidates, ...} }
    }
    """
    import intercepts

    indices = payload.get("indices")
    if indices is not None and not isinstance(indices, list):
        raise HTTPException(400, "indices must be a list of integers or null")
    k = int(payload.get("k", 30))
    over_fetch = int(payload.get("over_fetch", 200))
    weights = payload.get("weights")
    vector_threshold = payload.get("vector_threshold")
    if vector_threshold is not None:
        vector_threshold = float(vector_threshold)
    max_options = int(payload.get("max_options_per_question", 4))

    cfg = load_config()
    if not cfg.api_keys.openai:
        raise HTTPException(400, "OpenAI API key not configured (needed for embedding)")
    _require_workbench_spend(payload)

    result = await intercepts.analyze(
        indices=indices,
        k=k,
        over_fetch=over_fetch,
        openai_api_key=cfg.api_keys.openai,
        weights=weights,
        vector_threshold=vector_threshold,
        max_options_per_question=max_options,
    )
    return result


@app.post("/api/intercepts/analyze/stream")
async def api_intercepts_analyze_stream(payload: dict):
    """Streaming variant of /analyze for progress on the full 728 terms.

    Emits SSE events:
        intercept:start    {n_terms, k, over_fetch}
        intercept:row      {index, term, row}     -- one per term completed
        intercept:error    {index, term, error}
        intercept:done     {elapsed_seconds, n_rows}
    """
    import intercepts

    indices = payload.get("indices")
    k = int(payload.get("k", 30))
    over_fetch = int(payload.get("over_fetch", 200))
    weights = payload.get("weights")
    vector_threshold = payload.get("vector_threshold")
    if vector_threshold is not None:
        vector_threshold = float(vector_threshold)
    max_options = int(payload.get("max_options_per_question", 4))

    cfg = load_config()
    if not cfg.api_keys.openai:
        raise HTTPException(400, "OpenAI API key not configured")
    _require_workbench_spend(payload)

    terms = intercepts.list_terms()
    if indices is not None:
        selected = [t for t in terms if t["index"] in set(indices)]
    else:
        selected = terms

    async def generate():
        import time
        retriever = await intercepts.get_retriever(cfg.api_keys.openai)
        start = time.time()
        yield _sse("intercept:start", {"n_terms": len(selected), "k": k, "over_fetch": over_fetch})
        rows = []
        for t in selected:
            try:
                out = await intercepts.analyze_term(
                    retriever, t["term"], k, over_fetch, weights, vector_threshold,
                    max_options_per_question=max_options,
                )
            except Exception as exc:
                yield _sse("intercept:error", {"index": t["index"], "term": t["term"], "error": repr(exc)})
                continue
            row = out["row"]
            row["index"] = t["index"]
            row["count"] = t["count"]
            row["template"] = t["template"]
            row["source"] = t["source"]
            # NOTE: the 728 intercept list is HMRC-curated — keep its legacy
            # Generic/Hard-to-classify/Escalate template as the only label.
            # The action-bucket classifier (description.guidance/exclude/filter
            # /annotate_ai166_fix) is reserved for bucket-B commodity sweeps,
            # which run via /api/intercepts/analyze_commodities/stream.
            rows.append(row)
            yield _sse("intercept:row", {
                "index": t["index"],
                "term": t["term"],
                "row": row,
                "top_candidates": out["top_candidates"],
                "below_threshold_candidates": out.get("below_threshold_candidates", []),
                "retrieval_meta": out.get("retrieval_meta", {}),
            })
        yield _sse("intercept:done", {"elapsed_seconds": time.time() - start, "n_rows": len(rows)})

    return StreamingResponse(generate(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/intercepts/analyze_commodities/stream")
async def api_intercepts_analyze_commodities_stream(payload: dict):
    """Run the same complexity pipeline over every declarable UK commodity
    (or a sample). The commodity's own self_text/search_text/description is
    used as the retrieval query — so the KPI reflects "how ambiguous is this
    commodity's description when fed back to retrieval?".

    Request: {
        k, over_fetch, weights, vector_threshold, max_options_per_question,
        sample_size (optional — N evenly-spaced commodities for fast preview),
    }
    SSE events:
        commodity:start    {n_items, k, sample_size}
        commodity:row      {code, sid, query, chapter, section, row}
        commodity:error    {code, error}
        commodity:done     {elapsed_seconds, n_rows}
    """
    import intercepts

    k = int(payload.get("k", 30))
    over_fetch = int(payload.get("over_fetch", 200))
    weights = payload.get("weights")
    vector_threshold = payload.get("vector_threshold")
    if vector_threshold is not None:
        vector_threshold = float(vector_threshold)
    max_options = int(payload.get("max_options_per_question", 4))
    sample_size = payload.get("sample_size")
    if sample_size is not None:
        sample_size = int(sample_size)
    # New: query_strategy = "self_text" (neighbour density) or "paraphrase"
    # (classification difficulty). Default keeps the existing behaviour.
    query_strategy = payload.get("query_strategy", "self_text")
    if query_strategy not in ("self_text", "paraphrase"):
        raise HTTPException(400, "query_strategy must be 'self_text' or 'paraphrase'")

    cfg = load_config()
    if not cfg.api_keys.openai:
        raise HTTPException(400, "OpenAI API key not configured (needed for embedding)")
    _require_workbench_spend(payload)

    async def generate():
        import time
        retriever = await intercepts.get_retriever(cfg.api_keys.openai)
        start = time.time()
        # Pre-fetch the item list once so commodity:start can report a real count
        items = await intercepts.list_declarable_commodities(retriever, sample_size=sample_size)
        yield _sse("commodity:start", {
            "n_items": len(items),
            "k": k,
            "sample_size": sample_size,
            "max_options_per_question": max_options,
            "query_strategy": query_strategy,
        })

        rows = []
        n_rows = 0
        async for it, row, err in intercepts.analyze_all_commodities_iter(
            retriever, k, over_fetch, weights, vector_threshold,
            sample_size=sample_size,
            max_options_per_question=max_options,
            query_strategy=query_strategy,
            openai_api_key=cfg.api_keys.openai,
        ):
            if err is not None:
                yield _sse("commodity:error", {"code": it["code"], "error": err})
                continue
            rows.append(row)
            n_rows += 1
            # Ship top_candidates only for small/preview runs so SSE payloads
            # don't balloon at 14k scale. The DetailPanel falls back gracefully
            # when missing (Stats + entropy chart still render, no tree).
            include_cands = (sample_size is not None and sample_size <= 500)
            yield _sse("commodity:row", {
                "code": it["code"],
                "sid": it["sid"],
                "query": row.get("query", it["query"]),
                "chapter": it["chapter"],
                "section": it["section"],
                "row": row,
                "top_candidates": it.get("top_candidates", []) if include_cands else [],
            })
        yield _sse("commodity:done", {"elapsed_seconds": time.time() - start, "n_rows": n_rows})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/intercepts/generate-question")
async def api_intercepts_generate_question(payload: dict):
    """Generate the actual differentiator question the production LLM would
    ask at a given inflection node, using the same prompt template + a real
    GPT-5.5 call with medium reasoning effort.

    Request: {
        "term": str,
        "candidates": [{ "goods_nomenclature_item_id": str, "description": str, "score": float }, ...],
        "breadcrumb": [{ "level": str, "label": str, "description": str }, ...] (optional)
    }
    """
    import intercepts as _intercepts

    term = (payload.get("term") or "").strip()
    if not term:
        raise HTTPException(400, "term is required")
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list) or not candidates:
        raise HTTPException(400, "candidates must be a non-empty list")
    breadcrumb = payload.get("breadcrumb") or None

    cfg = load_config()
    if not cfg.api_keys.openai:
        raise HTTPException(400, "OpenAI API key not configured")
    _require_workbench_spend(payload)

    result = await _intercepts.generate_inflection_question(
        term=term,
        candidates=candidates,
        openai_api_key=cfg.api_keys.openai,
        breadcrumb=breadcrumb,
    )
    return result


@app.get("/api/intercepts/runs")
def api_intercepts_list_runs():
    import intercepts
    return intercepts.list_runs()


@app.get("/api/intercepts/runs/{run_id}")
def api_intercepts_load_run(run_id: str):
    import intercepts
    try:
        rec = intercepts.load_run(run_id)
    except intercepts.RunTooLarge as exc:
        raise HTTPException(413, str(exc))
    if rec is None:
        raise HTTPException(404, "Run not found")
    return rec


@app.get("/api/complexity/charts/{kind}")
def api_complexity_chart(kind: str, sweep_id: str):
    """Server-rendered chart PNG (matplotlib). Frontend just <img> tags it —
    avoids recharts choking on 14k+ DOM nodes. Cached on disk per sweep_id
    so the second view is instant."""
    if kind not in ("scatter", "density"):
        raise HTTPException(404, "Unknown chart kind")
    try:
        png = complexity_charts.get_chart(kind, sweep_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@app.get("/api/intercepts/runs/{run_id}/scatter")
def api_intercepts_run_scatter(run_id: str):
    """Tiny scatter-only payload — just (code, chapter, composite, bucket,
    gold_rank, term) per row. ~3MB for 14k commodities instead of 800MB.
    Backed by a pre-generated companion file (.scatter.json)."""
    import intercepts
    scatter_path = intercepts.RUNS_DIR / f"run_{run_id}.scatter.json"
    if not scatter_path.exists():
        raise HTTPException(404, "Scatter companion not found. Run make_scatter_companion.py.")
    return json.loads(scatter_path.read_text())


@app.get("/api/intercepts/runs/{run_id}/rows_only")
def api_intercepts_run_rows_only(run_id: str):
    """Lightweight: returns the run minus the per-row top_candidates.
    Lets the frontend load 14k-row sweeps without choking on 800MB of
    candidate-text payload. The DetailPanel can fetch one commodity's
    candidates separately via /candidates if needed."""
    import intercepts
    try:
        rec = intercepts.load_run(run_id)
    except intercepts.RunTooLarge as exc:
        raise HTTPException(413, str(exc))
    if rec is None:
        raise HTTPException(404, "Run not found")
    lite = {k: v for k, v in rec.items() if k != "details"}
    return lite


@app.get("/api/intercepts/runs/{run_id}/candidates/{code}")
def api_intercepts_run_candidates(run_id: str, code: str):
    """Return top_candidates for a single commodity from a saved run."""
    import intercepts
    try:
        rec = intercepts.load_run(run_id)
    except intercepts.RunTooLarge as exc:
        raise HTTPException(413, str(exc))
    if rec is None:
        raise HTTPException(404, "Run not found")
    det = (rec.get("details") or {}).get(code) or {}
    return {
        "code": code,
        "top_candidates": det.get("top_candidates", []),
        "row": det.get("row"),
    }


@app.get("/api/intercepts/runs/{run_id}/recall_summary")
def api_intercepts_recall_summary(run_id: str):
    """Aggregate a gold-recall run by chapter for charting.
    Returns small JSON instead of the full multi-MB run."""
    import intercepts
    try:
        rec = intercepts.load_run(run_id)
    except intercepts.RunTooLarge as exc:
        raise HTTPException(413, str(exc))
    if rec is None:
        raise HTTPException(404, "Run not found")

    rows = rec.get("rows", []) or []
    by_chapter: dict[str, dict] = {}
    total_pass = total_fail = 0
    rank_dist = {"1": 0, "2-5": 0, "6-10": 0, "11-30": 0, "miss": 0}

    for r in rows:
        code = (r.get("code") or "").strip()
        if len(code) < 2:
            continue
        chap = code[:2]
        ent = by_chapter.setdefault(chap, {"chapter": chap, "total": 0, "pass": 0, "fail": 0})
        ent["total"] += 1
        if r.get("recall_pass"):
            ent["pass"] += 1
            total_pass += 1
            rank = r.get("gold_rank") or 999
            if rank == 1:
                rank_dist["1"] += 1
            elif rank <= 5:
                rank_dist["2-5"] += 1
            elif rank <= 10:
                rank_dist["6-10"] += 1
            else:
                rank_dist["11-30"] += 1
        else:
            ent["fail"] += 1
            total_fail += 1
            rank_dist["miss"] += 1

    chapters = sorted(by_chapter.values(), key=lambda x: int(x["chapter"]) if x["chapter"].isdigit() else 999)
    for c in chapters:
        c["fail_rate"] = c["fail"] / c["total"] if c["total"] else 0
    return {
        "id": rec.get("id"),
        "name": rec.get("name"),
        "n_terms": rec.get("n_terms"),
        "total_pass": total_pass,
        "total_fail": total_fail,
        "rank_distribution": rank_dist,
        "recall_metrics": rec.get("recall_metrics"),
        "chapters": chapters,
    }


@app.post("/api/intercepts/runs/save")
def api_intercepts_save_run(payload: dict):
    """Persist a completed analysis run.

    Request: {"name": str, "result": <analyze response>}
    """
    import intercepts
    name = (payload.get("name") or "").strip() or "unnamed"
    result = payload.get("result")
    if not isinstance(result, dict):
        raise HTTPException(400, "result is required")
    run_id = intercepts.save_run(name, result)
    return {"id": run_id}


@app.post("/api/prompts/save")
async def api_prompts_save(payload: dict):
    """Append a new prompt to search_contexts.json.

    Request: {"raw_query": str, "processed_query": str, "formatted_results": [...]}
    Response: {"index": int, "total": int}
    """
    from pathlib import Path

    raw_query = str(payload.get("raw_query", "")).strip()
    results = payload.get("formatted_results", [])
    if not raw_query or not isinstance(results, list) or len(results) == 0:
        raise HTTPException(400, "raw_query and non-empty formatted_results required")

    data_path = Path(__file__).parent.parent / "data" / "search_contexts.json"
    data = json.loads(data_path.read_text())
    # New index = max existing + 1 so we don't collide with existing prompts
    max_idx = max((q["index"] for q in data["queries"]), default=0)
    new_idx = max_idx + 1
    data["queries"].append({
        "index": new_idx,
        "raw_query": raw_query,
        "processed_query": str(payload.get("processed_query") or raw_query),
        "result_count": len(results),
        "formatted_results": results,
    })
    data_path.write_text(json.dumps(data, indent=2))
    # Bust the in-memory cache so list_prompts reflects the new entry.
    from prompts import _cached_data  # noqa: F401 - exists for side-effect
    import prompts as prompts_mod
    prompts_mod._cached_data = None
    return {"index": new_idx, "total": len(data["queries"])}


@app.get("/api/prompts/{index}")
def api_prompt_detail(index: int):
    try:
        return get_prompt_detail(index)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


# --- ATaR ingestion + approval ---


@app.get("/api/atar/drafts")
def api_atar_list_drafts():
    """List every draft (pending + approved + discarded). UI filters client-side."""
    from atar import list_drafts
    return {"drafts": list_drafts()}


@app.get("/api/atar/drafts/{ref}")
def api_atar_get_draft(ref: str):
    from atar import get_draft
    d = get_draft(ref)
    if d is None:
        raise HTTPException(404, f"draft {ref} not found")
    return d


@app.post("/api/atar/ingest")
async def api_atar_ingest(payload: dict):
    """Scrape + extract + retrieve OS context for N rulings (or a list of refs).

    Request: {"count": 20} or {"refs": ["600014923", ...]}
              + optional "opensearch_limit" (default 80)
    Response: {"ingested": [...drafts], "skipped": [...refs]}

    Idempotent: refs that already have a draft are skipped, not re-scraped.
    """
    from openai import AsyncOpenAI

    from atar import ingest_batch, list_drafts

    cfg = load_config()
    if not cfg.api_keys.openai:
        raise HTTPException(400, "OpenAI API key required (used for fact extraction + embeddings)")
    _require_workbench_spend(payload)

    refs = payload.get("refs")
    count = int(payload.get("count", 20))
    os_limit = max(10, min(200, int(payload.get("opensearch_limit", 80))))

    if refs is not None and not isinstance(refs, list):
        raise HTTPException(400, "refs must be a list of strings")

    pre_existing = {d["ref"] for d in list_drafts()}
    client = AsyncOpenAI(api_key=cfg.api_keys.openai)
    new_drafts = await ingest_batch(
        client,
        refs=[str(r) for r in refs] if refs else None,
        count=count,
        opensearch_limit=os_limit,
    )
    return {
        "ingested": new_drafts,
        "ingested_count": len(new_drafts),
        "skipped_existing": sorted(pre_existing),
    }


@app.post("/api/atar/drafts/{ref}/regenerate-facts")
async def api_atar_regenerate_facts(ref: str, payload: dict | None = None):
    """Re-run the LLM fact extractor on an existing draft (e.g. user edited
    fact slot vocabulary and wants a fresh suggestion). Returns the new
    gold_facts. Does NOT save - the UI saves via the patch endpoint after
    review."""
    from openai import AsyncOpenAI

    from atar import AtarRuling, extract_facts, get_draft

    d = get_draft(ref)
    if d is None:
        raise HTTPException(404, f"draft {ref} not found")
    cfg = load_config()
    if not cfg.api_keys.openai:
        raise HTTPException(400, "OpenAI API key required")
    _require_workbench_spend(payload or {})

    ruling = AtarRuling(**d["ruling"])
    client = AsyncOpenAI(api_key=cfg.api_keys.openai)
    new_facts = await extract_facts(client, ruling)
    return {"ref": ref, "gold_facts": new_facts}


@app.patch("/api/atar/drafts/{ref}")
def api_atar_patch_draft(ref: str, payload: dict):
    """Save user edits (typically gold_facts) to a draft before approval."""
    from atar import update_draft_fields
    allowed = {"gold_facts", "raw_query", "oracle_text", "gold_code"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if not fields:
        raise HTTPException(400, f"no editable fields; allowed: {sorted(allowed)}")
    if "gold_facts" in fields:
        cleaned = []
        for f in fields["gold_facts"] or []:
            if not isinstance(f, dict):
                continue
            slot = str(f.get("slot", "")).strip()
            answer = str(f.get("answer", "")).strip()
            if slot and answer:
                cleaned.append({"slot": slot, "answer": answer})
        fields["gold_facts"] = cleaned
    updated = update_draft_fields(ref, **fields)
    if updated is None:
        raise HTTPException(404, f"draft {ref} not found")
    return updated


@app.post("/api/atar/drafts/{ref}/approve")
def api_atar_approve(ref: str, payload: dict | None = None):
    """Promote a draft to a real prompt in search_contexts.json."""
    from atar import approve_draft
    payload = payload or {}
    override = payload.get("gold_facts")
    try:
        return approve_draft(ref, override_facts=override if isinstance(override, list) else None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/atar/drafts/{ref}")
def api_atar_discard(ref: str):
    from atar import discard_draft
    if not discard_draft(ref):
        raise HTTPException(404, f"draft {ref} not found")
    return {"ref": ref, "status": "discarded"}


# --- Benchmark ---


@app.post("/api/benchmark/start")
async def api_start_benchmark(req: BenchmarkRequest):
    _require_workbench_spend(req)
    cfg = load_config()

    async def event_stream():
        async for event in run_benchmark(req.prompt_indices, req.model_ids, cfg, req.opensearch_limit):
            data = json.dumps(event.data)
            yield f"event: {event.event}\ndata: {data}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/benchmark/cancel")
def api_benchmark_cancel():
    """Signal the running benchmark to stop. True server-side cancellation -
    in-flight asyncio tasks are cancelled, no further provider/judge calls
    are dispatched, and the run is saved with status='cancelled'."""
    delivered = cancel_current_run()
    return {"cancelled": delivered}


@app.get("/api/benchmark/status")
def api_benchmark_status():
    run = get_current_run()
    if run is None:
        return {"status": "idle"}
    return {
        "id": run.id,
        "status": run.status,
        "progress": run.progress,
        "baseline_count": len(run.baseline_results),
        "model_count": len(run.model_results),
        "evaluation_count": len(run.evaluations),
    }


@app.get("/api/benchmark/runs")
def api_list_runs():
    return list_saved_runs()


def _serialise_run(run) -> dict:
    """Shared serialiser so /results and /runs/{id} return the same shape."""
    def enrich(results):
        out = []
        for r in results:
            d = r.model_dump()
            d["response_type"] = detect_response_type(r)
            out.append(d)
        return out

    return {
        "id": run.id,
        "timestamp": run.timestamp,
        "status": run.status,
        "opensearch_limit": run.opensearch_limit,
        "baseline_model_id": run.baseline_model_id,
        "panel_model_ids": run.panel_model_ids,
        "panel_results": enrich(run.panel_results),
        "consensus_results": enrich(run.consensus_results),
        "baseline_results": enrich(run.baseline_results),
        "model_results": enrich(run.model_results),
        "evaluations": [e.model_dump() for e in run.evaluations],
        "summaries": [s.model_dump() for s in run.summaries],
        "prompt_indices": run.prompt_indices,
        "model_ids": run.model_ids,
        "fact_store": run.fact_store,
        "prompt_sections": run.prompt_sections,
    }


@app.get("/api/benchmark/runs/{run_id}")
def api_get_run(run_id: str):
    run = load_saved_run(run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return _serialise_run(run)


@app.get("/api/benchmark/results")
def api_benchmark_results():
    run = get_current_run()
    if run is None:
        raise HTTPException(404, "No benchmark run available")
    return _serialise_run(run)


@app.get("/api/benchmark/export/json")
def api_export_json():
    run = get_current_run()
    if run is None:
        raise HTTPException(404, "No benchmark run")

    filename = f"benchmark_{run.id}.json"
    path = RESULTS_DIR / filename
    path.write_text(run.model_dump_json(indent=2))

    return StreamingResponse(
        io.BytesIO(path.read_bytes()),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/benchmark/export/csv")
def api_export_csv():
    run = get_current_run()
    if run is None:
        raise HTTPException(404, "No benchmark run")

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Model ID",
        "Model Name",
        "Prompt Index",
        "Cosine Similarity",
        "Code Match Score",
        "Top-1 Match",
        "Top-5 Overlap",
        "Delta Score",
        "Total Latency (ms)",
        "Baseline Total Latency (ms)",
        "Speed Factor",
        "Total Cost ($)",
        "Baseline Total Cost ($)",
        "Rounds",
        "Baseline Rounds",
        "Response Type",
    ])

    model_names = {}
    cfg = load_config()
    for m in cfg.models:
        model_names[m.id] = m.name

    model_response_types = {}
    for r in run.model_results:
        model_response_types[(r.model_id, r.prompt_index)] = detect_response_type(r)

    for ev in run.evaluations:
        rt = model_response_types.get((ev.model_id, ev.prompt_index), "unknown")
        writer.writerow([
            ev.model_id,
            model_names.get(ev.model_id, ev.model_id),
            ev.prompt_index,
            ev.cosine_similarity,
            ev.code_match_score,
            ev.top1_match,
            ev.top5_overlap,
            ev.delta_score,
            ev.total_latency_ms,
            ev.baseline_total_latency_ms,
            ev.speed_factor,
            ev.total_cost,
            ev.baseline_total_cost,
            ev.total_rounds,
            ev.baseline_total_rounds,
            rt,
        ])

    # Summary section
    writer.writerow([])
    writer.writerow(["--- Model Summaries ---"])
    writer.writerow([
        "Model ID", "Model Name", "Avg Cosine Sim", "Avg Code Match",
        "Avg Delta Score", "Avg Total Latency (ms)", "Avg Speed Factor",
        "Total Cost ($)", "Avg Cost/Classification", "Top-1 Accuracy", "Avg Top-5 Overlap",
        "Avg Rounds",
    ])
    for s in run.summaries:
        writer.writerow([
            s.model_id, s.model_name, s.avg_cosine_similarity, s.avg_code_match_score,
            s.avg_delta_score, s.avg_total_latency_ms, s.avg_speed_factor,
            s.total_cost, s.avg_cost_per_classification, s.top1_accuracy, s.avg_top5_overlap,
            s.avg_rounds,
        ])

    filename = f"benchmark_{run.id}.csv"
    path = RESULTS_DIR / filename
    path.write_text(output.getvalue())

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/api/eval-cost/summary")
async def api_eval_cost_summary(limit: int = 20):
    limit = max(1, min(limit, 100))
    pool = await kg._get_pool()
    async with pool.acquire() as conn:
        table_name = f"{kg.KG_SCHEMA}.commodity_fact_model_eval"
        table_exists = await conn.fetchval("SELECT to_regclass($1)", table_name)
        if not table_exists:
            return {
                "totals": {
                    "calls": 0,
                    "ok": 0,
                    "failed": 0,
                    "runs": 0,
                    "models": 0,
                    "prompt_versions": 0,
                    "cost_usd": 0.0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                },
                "runs": [],
                "model_totals": [],
                "prompt_totals": [],
            }

        totals = await conn.fetchrow(
            f"""
            SELECT count(*)::int AS calls,
                   count(*) FILTER (WHERE error IS NULL)::int AS ok,
                   count(*) FILTER (WHERE error IS NOT NULL)::int AS failed,
                   count(DISTINCT run_id)::int AS runs,
                   count(DISTINCT model)::int AS models,
                   count(DISTINCT prompt_version)::int AS prompt_versions,
                   coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                   coalesce(sum(prompt_tokens), 0)::bigint AS prompt_tokens,
                   coalesce(sum(completion_tokens), 0)::bigint AS completion_tokens,
                   min(created_at) AS first_write,
                   max(created_at) AS last_write
            FROM {kg.KG_SCHEMA}.commodity_fact_model_eval
            """
        )
        runs = await conn.fetch(
            f"""
            SELECT run_id,
                   count(*)::int AS calls,
                   count(*) FILTER (WHERE error IS NULL)::int AS ok,
                   count(*) FILTER (WHERE error IS NOT NULL)::int AS failed,
                   count(DISTINCT model)::int AS models,
                   count(DISTINCT prompt_version)::int AS prompt_versions,
                   count(DISTINCT commodity_code)::int AS commodity_codes,
                   coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                   coalesce(sum(prompt_tokens), 0)::bigint AS prompt_tokens,
                   coalesce(sum(completion_tokens), 0)::bigint AS completion_tokens,
                   avg(nullif(quality->>'score', '')::float8)::float8 AS avg_score,
                   min(created_at) AS first_write,
                   max(created_at) AS last_write,
                   extract(epoch FROM max(created_at) - min(created_at))::float8 AS duration_seconds
            FROM {kg.KG_SCHEMA}.commodity_fact_model_eval
            GROUP BY run_id
            ORDER BY max(created_at) DESC
            LIMIT $1
            """,
            limit,
        )
        model_totals = await conn.fetch(
            f"""
            SELECT model,
                   count(*)::int AS calls,
                   count(*) FILTER (WHERE error IS NULL)::int AS ok,
                   coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                   coalesce(sum(prompt_tokens), 0)::bigint AS prompt_tokens,
                   coalesce(sum(completion_tokens), 0)::bigint AS completion_tokens,
                   avg(nullif(quality->>'score', '')::float8)::float8 AS avg_score
            FROM {kg.KG_SCHEMA}.commodity_fact_model_eval
            GROUP BY model
            ORDER BY cost_usd DESC
            """
        )
        prompt_totals = await conn.fetch(
            f"""
            SELECT prompt_version,
                   count(*)::int AS calls,
                   count(*) FILTER (WHERE error IS NULL)::int AS ok,
                   coalesce(sum(cost_usd), 0)::float8 AS cost_usd,
                   avg(nullif(quality->>'score', '')::float8)::float8 AS avg_score
            FROM {kg.KG_SCHEMA}.commodity_fact_model_eval
            GROUP BY prompt_version
            ORDER BY cost_usd DESC
            LIMIT 50
            """
        )


        embedding_cost_per_million = float(os.environ.get("COST_EMBEDDING_USD_PER_1M_TOKENS", "0.02"))
        e2e_provider_call_est_usd = float(os.environ.get("COST_E2E_PROVIDER_CALL_USD", "0.002"))

        retrieval_runs = []
        retrieval_totals = {
            "runs": 0,
            "calls": 0,
            "estimated_embedding_tokens": 0,
            "estimated_cost_usd": 0.0,
            "last_write": None,
        }
        retrieval_table_exists = await conn.fetchval("SELECT to_regclass($1)", f"{kg.KG_SCHEMA}.eval_runs")
        if retrieval_table_exists:
            raw_retrieval_runs = await conn.fetch(
                f"""
                SELECT er.id,
                       er.run_label,
                       er.config_json,
                       er.n_queries,
                       er.retrieval_limit,
                       count(rr.id)::int AS calls,
                       coalesce(sum(greatest(1, ceil(length(coalesce(g.query, '')) / 4.0))), 0)::bigint AS estimated_embedding_tokens,
                       er.started_at AS first_write,
                       er.finished_at AS last_write,
                       extract(epoch FROM er.finished_at - er.started_at)::float8 AS duration_seconds
                FROM {kg.KG_SCHEMA}.eval_runs er
                LEFT JOIN {kg.KG_SCHEMA}.eval_run_results rr ON rr.run_id = er.id
                LEFT JOIN {kg.KG_SCHEMA}.eval_gold g ON g.id = rr.gold_id
                GROUP BY er.id
                ORDER BY er.started_at DESC
                """
            )
            for row in raw_retrieval_runs:
                item = dict(row)
                cfg = item.pop("config_json") or {}
                item["use_vector"] = bool(cfg.get("use_vector"))
                item["use_facts_vec"] = bool(cfg.get("use_facts_vec"))
                item["use_kg_vec"] = bool(cfg.get("use_kg_vec"))
                vector_enabled = item["use_vector"] or item["use_facts_vec"] or item["use_kg_vec"]
                tokens = int(item.get("estimated_embedding_tokens") or 0) if vector_enabled else 0
                item["estimated_embedding_tokens"] = tokens
                item["estimated_cost_usd"] = tokens * embedding_cost_per_million / 1_000_000.0
                retrieval_runs.append(item)
            retrieval_totals = {
                "runs": len(retrieval_runs),
                "calls": sum(int(row.get("calls") or 0) for row in retrieval_runs),
                "estimated_embedding_tokens": sum(int(row.get("estimated_embedding_tokens") or 0) for row in retrieval_runs),
                "estimated_cost_usd": sum(float(row.get("estimated_cost_usd") or 0.0) for row in retrieval_runs),
                "last_write": max((row.get("last_write") for row in retrieval_runs if row.get("last_write")), default=None),
            }
            retrieval_runs = retrieval_runs[:limit]

        e2e_runs = []
        e2e_totals = {
            "runs": 0,
            "provider_calls": 0,
            "estimated_embedding_tokens": 0,
            "estimated_cost_usd": 0.0,
            "last_write": None,
        }
        e2e_table_exists = await conn.fetchval("SELECT to_regclass($1)", f"{kg.KG_SCHEMA}.e2e_eval_runs")
        if e2e_table_exists:
            raw_e2e_runs = await conn.fetch(
                f"""
                SELECT r.id,
                       r.run_label,
                       r.retrieval_run_label,
                       r.question_mode,
                       r.answerer,
                       r.config_json,
                       r.input_count,
                       r.provider_calls_used,
                       coalesce(sum(greatest(1, ceil(length(coalesce(res.query, '')) / 4.0))), 0)::bigint AS estimated_embedding_tokens,
                       r.started_at AS first_write,
                       r.finished_at AS last_write,
                       extract(epoch FROM r.finished_at - r.started_at)::float8 AS duration_seconds
                FROM {kg.KG_SCHEMA}.e2e_eval_runs r
                LEFT JOIN {kg.KG_SCHEMA}.e2e_eval_results res ON res.run_id = r.id
                GROUP BY r.id
                ORDER BY r.started_at DESC
                """
            )
            for row in raw_e2e_runs:
                item = dict(row)
                cfg = item.pop("config_json") or {}
                retrieval_cfg = cfg.get("retrieval_config") or {}
                vector_enabled = bool(
                    retrieval_cfg.get("use_vector")
                    or retrieval_cfg.get("use_facts_vec")
                    or retrieval_cfg.get("use_kg_vec")
                )
                tokens = int(item.get("estimated_embedding_tokens") or 0) if vector_enabled else 0
                provider_calls = int(item.get("provider_calls_used") or 0)
                item["estimated_embedding_tokens"] = tokens
                item["estimated_cost_usd"] = (
                    provider_calls * e2e_provider_call_est_usd
                    + tokens * embedding_cost_per_million / 1_000_000.0
                )
                e2e_runs.append(item)
            e2e_totals = {
                "runs": len(e2e_runs),
                "provider_calls": sum(int(row.get("provider_calls_used") or 0) for row in e2e_runs),
                "estimated_embedding_tokens": sum(int(row.get("estimated_embedding_tokens") or 0) for row in e2e_runs),
                "estimated_cost_usd": sum(float(row.get("estimated_cost_usd") or 0.0) for row in e2e_runs),
                "last_write": max((row.get("last_write") for row in e2e_runs if row.get("last_write")), default=None),
            }
            e2e_runs = e2e_runs[:limit]

        classification_runs = []
        classification_totals = {
            "runs": 0,
            "sessions": 0,
            "estimated_cost_usd": 0.0,
            "last_write": None,
        }
        classify_table_exists = await conn.fetchval("SELECT to_regclass($1)", f"{kg.KG_SCHEMA}.classify_runs")
        if classify_table_exists:
            classification_runs = [dict(row) for row in await conn.fetch(
                f"""
                SELECT run_label,
                       model,
                       strategy,
                       prompt_mode,
                       augmentation,
                       count(*)::int AS sessions,
                       coalesce(sum(est_cost_usd), 0)::float8 AS estimated_cost_usd,
                       min(started_at) AS first_write,
                       max(started_at) AS last_write
                FROM {kg.KG_SCHEMA}.classify_runs
                GROUP BY run_label, model, strategy, prompt_mode, augmentation
                ORDER BY max(started_at) DESC
                LIMIT $1
                """,
                limit,
            )]
            classification_totals = dict(await conn.fetchrow(
                f"""
                SELECT count(DISTINCT run_label)::int AS runs,
                       count(*)::int AS sessions,
                       coalesce(sum(est_cost_usd), 0)::float8 AS estimated_cost_usd,
                       max(started_at) AS last_write
                FROM {kg.KG_SCHEMA}.classify_runs
                """
            ))

    def encode_row(row):
        out = dict(row)
        for key in ("first_write", "last_write"):
            if out.get(key) is not None:
                out[key] = out[key].isoformat()
        return out

    fact_cost = float((totals or {}).get("cost_usd") or 0.0)
    retrieval_cost = float((retrieval_totals or {}).get("estimated_cost_usd") or 0.0)
    e2e_cost = float((e2e_totals or {}).get("estimated_cost_usd") or 0.0)
    classification_cost = float((classification_totals or {}).get("estimated_cost_usd") or 0.0)

    return {
        "totals": encode_row(totals),
        "runs": [encode_row(row) for row in runs],
        "model_totals": [encode_row(row) for row in model_totals],
        "prompt_totals": [encode_row(row) for row in prompt_totals],
        "spend_totals": {
            "fact_eval_cost_usd": fact_cost,
            "retrieval_embedding_est_cost_usd": retrieval_cost,
            "e2e_est_cost_usd": e2e_cost,
            "classification_est_cost_usd": classification_cost,
            "estimated_total_usd": fact_cost + retrieval_cost + e2e_cost + classification_cost,
            "embedding_cost_per_million_tokens": embedding_cost_per_million,
            "e2e_provider_call_est_usd": e2e_provider_call_est_usd,
        },
        "retrieval": {
            "totals": encode_row(retrieval_totals),
            "runs": [encode_row(row) for row in retrieval_runs],
        },
        "e2e": {
            "totals": encode_row(e2e_totals),
            "runs": [encode_row(row) for row in e2e_runs],
        },
        "classification": {
            "totals": encode_row(classification_totals),
            "runs": [encode_row(row) for row in classification_runs],
        },
    }


# --- Knowledge Base (kg.* schema in tariff_db) -----------------------

@app.get("/api/kg/coverage")
async def kg_coverage():
    return await kg.coverage_stats()


@app.get("/api/kg/facets")
async def kg_list_facets(
    chapter: str | None = None,
    source: str | None = None,
    facet_key: str | None = None,
    use_scope: str | None = None,
    evidence_role: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    return await kg.list_facets(
        chapter=chapter, source=source, facet_key=facet_key,
        use_scope=use_scope, evidence_role=evidence_role, q=q,
        limit=limit, offset=offset,
    )


@app.get("/api/kg/facet_definitions")
async def kg_facet_definitions():
    return await kg.list_facet_definitions()


@app.patch("/api/kg/facets/{facet_id}")
async def kg_update_facet(facet_id: int, payload: dict):
    row = await kg.update_facet(
        facet_id,
        value=payload.get("facet_value"),
        confidence=payload.get("confidence"),
        source=payload.get("source"),
        use_scopes=payload.get("use_scopes"),
        evidence_roles=payload.get("evidence_roles"),
    )
    if not row:
        raise HTTPException(404, "Facet not found")
    return row


@app.delete("/api/kg/facets/{facet_id}")
async def kg_delete_facet(facet_id: int):
    ok = await kg.delete_facet(facet_id)
    if not ok:
        raise HTTPException(404, "Facet not found")
    return {"ok": True}


@app.get("/api/kg/edges")
async def kg_list_edges(
    scope: str | None = None,
    type: str | None = None,
    use_scope: str | None = None,
    evidence_role: str | None = None,
    q: str | None = None,
    limit: int = 200,
    offset: int = 0,
):
    return await kg.list_edges(
        scope=scope, type_=type, use_scope=use_scope, evidence_role=evidence_role,
        q=q, limit=limit, offset=offset,
    )


@app.get("/api/kg/edges/{edge_id}")
async def kg_get_edge(edge_id: str):
    row = await kg.get_edge(edge_id)
    if not row:
        raise HTTPException(404, "Edge not found")
    return row


@app.patch("/api/kg/edges/{edge_id}")
async def kg_update_edge(edge_id: str, payload: dict):
    row = await kg.update_edge(
        edge_id,
        title=payload.get("title"),
        body=payload.get("body"),
        scope=payload.get("scope"),
        type_=payload.get("type"),
        use_scopes=payload.get("use_scopes"),
        evidence_roles=payload.get("evidence_roles"),
    )
    if not row:
        raise HTTPException(404, "Edge not found")
    return row


@app.delete("/api/kg/edges/{edge_id}")
async def kg_delete_edge(edge_id: str):
    ok = await kg.delete_edge(edge_id)
    if not ok:
        raise HTTPException(404, "Edge not found")
    return {"ok": True}


@app.get("/api/kg/commodity/{code}")
async def kg_commodity(code: str):
    return await kg.commodity_view(code)


@app.get("/api/kg/audit/recent")
async def kg_audit_recent(limit: int = 100, action: str | None = None):
    return await kg.audit_log_recent(limit=limit, action=action)


@app.get("/api/kg/audit/{table_name}/{row_id}")
async def kg_audit_row(table_name: str, row_id: str, limit: int = 50):
    return await kg.audit_log_for(table_name=table_name, row_id=row_id, limit=limit)


@app.get("/api/kg/graph")
async def kg_graph(
    focus_code: str | None = None,
    chapter: str | None = None,
    all_mode: bool = False,
    rule_types: str | None = None,
    scopes: str | None = None,
    include_gaps: bool = False,
    gap_chapters: str | None = None,
    max_nodes: int = 200,
):
    return await kg.graph_elements(
        focus_code=focus_code,
        chapter=chapter,
        all_mode=all_mode,
        rule_types=rule_types.split(",") if rule_types else None,
        scopes=scopes.split(",") if scopes else None,
        include_gaps=include_gaps,
        gap_chapters=gap_chapters.split(",") if gap_chapters else None,
        max_nodes=max_nodes,
    )



# --- Live Q&A / E2E matrices -------------------------------------------

def _html_escape(value) -> str:
    from html import escape

    return escape(str(value if value is not None else ""), quote=True)


def _pct(num, den) -> str:
    try:
        den = int(den or 0)
        return "-" if den <= 0 else f"{(100 * int(num or 0) / den):.1f}%"
    except Exception:
        return "-"


def _num(value) -> str:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "-"


def _e2e_matrix_rows(limit: int = 160) -> tuple[list[dict], str]:
    try:
        import psycopg
        from psycopg.rows import dict_row

        dsn = os.environ.get("TARIFF_DB_DSN", "postgresql:///tariff_db")
        with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s) AS runs_table", (f"{kg.KG_SCHEMA}.e2e_eval_runs",))
            table_row = cur.fetchone() or {}
            if not table_row.get("runs_table"):
                return [], ""
            cur.execute(
                f"""
                SELECT r.id,
                       r.run_label,
                       r.retrieval_run_label,
                       r.question_mode,
                       r.answerer,
                       r.question_model,
                       r.simulator_model,
                       r.pair_limit,
                       r.persona_count,
                       r.input_count,
                       r.retrieval_limit,
                       r.hydrate_limit,
                       r.max_rounds,
                       r.allow_spend,
                       r.config_json,
                       r.started_at,
                       r.finished_at,
                       r.n_inputs,
                       r.initial_gold_in_retrieval,
                       r.gold_kept,
                       r.gold_top1_after_qa,
                       r.avg_initial_rank,
                       r.avg_post_qa_rank,
                       r.avg_rounds,
                       r.avg_active_count,
                       r.provider_calls_used,
                       r.errors,
                       count(res.id)::int AS result_rows,
                       count(res.id) FILTER (WHERE res.final_state::text LIKE '%From retrieval%')::int AS fallback_rows,
                       coalesce(sum(
                           CASE
                             WHEN jsonb_typeof(res.final_state->'fallback_to_retrieval_rounds') = 'number'
                             THEN (res.final_state->>'fallback_to_retrieval_rounds')::int
                             ELSE 0
                           END
                       ), 0)::int AS fallback_rounds
                FROM {kg.KG_SCHEMA}.e2e_eval_runs r
                LEFT JOIN {kg.KG_SCHEMA}.e2e_eval_results res ON res.run_id = r.id
                GROUP BY r.id
                ORDER BY r.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()], ""
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _run_prompt_label(row: dict) -> str:
    cfg = row.get("config_json") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    prompt = cfg.get("staging_prompt_mode") or cfg.get("prompt_mode") or "-"
    effort = cfg.get("classify_reasoning_effort") or "-"
    policy = ", ".join(cfg.get("policy_eval") or []) if isinstance(cfg.get("policy_eval"), list) else ""
    parts = [f"prompt: {prompt}", f"reasoning: {effort}"]
    if policy:
        parts.append(f"policy: {policy}")
    return " | ".join(parts)


def _render_e2e_matrix(*, qa_only: bool) -> str:
    esc = _html_escape
    rows, error = _e2e_matrix_rows()
    title = "Q&A Matrix" if qa_only else "End-to-End Journey Matrix"
    sub = (
        "Question-mode comparison after retrieval. Keep/top1 rates are conditioned on gold being present in the retrieved shortlist."
        if qa_only
        else "Full journey view from retrieval through Q&A and final result-list preservation/rank metrics."
    )
    if error:
        body = f"<div class='empty'>Could not load matrix: {esc(error)}</div>"
    elif not rows:
        body = "<div class='empty'>No E2E/Q&A runs yet.</div>"
    else:
        html_rows = []
        for r in rows:
            n = r.get("n_inputs") or r.get("input_count") or r.get("result_rows") or 0
            eligible = r.get("initial_gold_in_retrieval") or 0
            denom = eligible if qa_only else n
            fallback = int(r.get("fallback_rows") or 0)
            fallback_rounds = int(r.get("fallback_rounds") or 0)
            fallback_cls = "bad" if fallback else "muted"
            done = bool(r.get("finished_at"))
            status_cls = "good" if done and not int(r.get("errors") or 0) else ("warn" if not done else "bad")
            html_rows.append(
                f"""
                <tr>
                  <td class='id'>#{esc(r.get('id'))}</td>
                  <td>
                    <b>{esc(r.get('run_label'))}</b>
                    <br><span>{esc(r.get('retrieval_run_label'))}</span>
                    <br><code>{esc(_run_prompt_label(r))}</code>
                  </td>
                  <td>{esc(r.get('question_mode'))}<br><span>{esc(r.get('answerer'))}</span></td>
                  <td>{esc(r.get('question_model') or '-')}<br><span>sim: {esc(r.get('simulator_model') or '-')}</span></td>
                  <td>{esc(n)}<br><span>{esc(eligible)} eligible</span></td>
                  <td>{_pct(r.get('initial_gold_in_retrieval'), n)}</td>
                  <td>{_pct(r.get('gold_kept'), denom)}</td>
                  <td>{_pct(r.get('gold_top1_after_qa'), denom)}</td>
                  <td>{_num(r.get('avg_initial_rank'))} -> {_num(r.get('avg_post_qa_rank'))}</td>
                  <td>{_num(r.get('avg_rounds'))}<br><span>active {_num(r.get('avg_active_count'))}</span></td>
                  <td>{esc(r.get('provider_calls_used') or 0)}</td>
                  <td class='{fallback_cls}'>{esc(fallback)} rows<br><span>{esc(fallback_rounds)} rounds</span></td>
                  <td class='{status_cls}'>{'done' if done else 'running'}<br><span>{esc(r.get('errors') or 0)} errors</span></td>
                </tr>
                """
            )
        body = f"""
        <table>
          <thead><tr>
            <th>Run</th><th>Config</th><th>Question mode</th><th>Models</th><th>N</th>
            <th>Gold in retrieval</th><th>Gold kept</th><th>Gold top1</th>
            <th>Avg rank</th><th>Rounds / active</th><th>Calls</th><th>Fallback</th><th>Status</th>
          </tr></thead>
          <tbody>{''.join(html_rows)}</tbody>
        </table>
        """
    return f"""
    <!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title>
    <style>
      body {{ margin:0; background:#070b13; color:#e5e7eb; font-family:Inter,system-ui,sans-serif; padding:24px; }}
      h1 {{ margin:0 0 6px; font-size:24px; }}
      .sub, span {{ color:#94a3b8; }}
      table {{ width:100%; border-collapse:separate; border-spacing:0; margin-top:18px; font-size:13px; }}
      th,td {{ border-bottom:1px solid #243044; padding:10px; text-align:left; vertical-align:top; }}
      th {{ background:#101827; color:#bfdbfe; position:sticky; top:0; z-index:1; }}
      tbody tr:nth-child(even) td {{ background:#0b1220; }}
      tbody tr:hover td {{ background:#111a2b; }}
      .id {{ color:#93c5fd; font-weight:800; white-space:nowrap; }}
      code {{ color:#c4b5fd; font-size:11px; }}
      .good {{ color:#bbf7d0; }}
      .warn {{ color:#fde68a; }}
      .bad {{ color:#fca5a5; }}
      .muted {{ color:#94a3b8; }}
      .empty {{ border:1px solid #263243; background:#0b1220; padding:18px; margin-top:18px; }}
    </style></head><body>
      <h1>{esc(title)}</h1>
      <div class='sub'>{esc(sub)}</div>
      {body}
    </body></html>
    """


@app.get("/eval/e2e-matrix")
def live_e2e_matrix() -> Response:
    return Response(_render_e2e_matrix(qa_only=False), media_type="text/html")


@app.get("/eval/qa-matrix")
def live_qa_matrix() -> Response:
    return Response(_render_e2e_matrix(qa_only=True), media_type="text/html")

# --- Static retrieval matrix -------------------------------------------

MATRIX_DIR = Path(__file__).parent.parent / "data" / "matrix"


@app.get("/eval/matrix")
def exported_matrix():
    matrix_path = MATRIX_DIR / "retrieval_matrix.html"
    if not matrix_path.exists():
        raise HTTPException(404, "Exported matrix snapshot is missing")
    return FileResponse(matrix_path, media_type="text/html")


@app.get("/eval/matrix.csv")
def exported_matrix_csv():
    csv_path = MATRIX_DIR / "retrieval_matrix.csv"
    if not csv_path.exists():
        raise HTTPException(404, "Exported matrix CSV is missing")
    return FileResponse(csv_path, media_type="text/csv", filename="retrieval_matrix.csv")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.environ.get("AI_FAN_OUT_HOST", "127.0.0.1"),
        port=int(os.environ.get("AI_FAN_OUT_PORT", "8000")),
        reload=True,
    )
