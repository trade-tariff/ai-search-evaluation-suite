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
    if os.environ.get("AI_FAN_OUT_WORKBENCH_SPEND_ENABLED", "").strip() == "1":
        return True
    if isinstance(payload, dict):
        return payload.get("allow_spend") is True
    return bool(getattr(payload, "allow_spend", False))


def _require_workbench_spend(payload: dict | object | None = None) -> None:
    if not _workbench_spend_enabled(payload):
        raise HTTPException(
            403,
            "Provider-backed workbench action blocked. Pass allow_spend=true "
            "for this request or set AI_FAN_OUT_WORKBENCH_SPEND_ENABLED=1.",
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
    rec = intercepts.load_run(run_id)
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
    rec = intercepts.load_run(run_id)
    if rec is None:
        raise HTTPException(404, "Run not found")
    lite = {k: v for k, v in rec.items() if k != "details"}
    return lite


@app.get("/api/intercepts/runs/{run_id}/candidates/{code}")
def api_intercepts_run_candidates(run_id: str, code: str):
    """Return top_candidates for a single commodity from a saved run."""
    import intercepts
    rec = intercepts.load_run(run_id)
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
    rec = intercepts.load_run(run_id)
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


# --- Single-backend consolidation: mount the trader-journey routes ------

from journey import main as _journey_main  # noqa: E402

_existing_paths = {getattr(r, "path", None) for r in app.router.routes}
for _r in _journey_main.app.router.routes:
    _p = getattr(_r, "path", "")
    if _p.startswith(("/api/", "/eval")) and _p not in _existing_paths:
        app.router.routes.append(_r)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.environ.get("AI_FAN_OUT_HOST", "127.0.0.1"),
        port=int(os.environ.get("AI_FAN_OUT_PORT", "8000")),
        reload=True,
    )
