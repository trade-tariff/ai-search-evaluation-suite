"""FastAPI entry-point for the trader-journey POC."""
from __future__ import annotations

import os
import queue
import threading
import uuid
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from auth import auth_enabled, install_optional_auth
from config import load_config
from .classification import (
    classify_step,
    eliminate_step,
    hydrated_candidate_qa,
    initial_candidates_for_eliminate,
    load_kg_edges,
    normalize_candidate_question_mode,
)
from . import local_db
from . import cost
from .duty import calculate_duty, commodity_lookup, commodity_requirements, country_list
from .declaration import build_declaration
from .examples import default_classify_config, list_examples
from .explainer import explain_duty
from .hydration import hydrate_commodity
from .inference import infer_duty_inputs
from .landed import calculate_landed
from .provider_guard import provider_calls_allowed
from .schemas import (
    ClassifyAnswerRequest,
    ClassifyStartRequest,
    ClassifyTurn,
    CandidateHydrationRequest,
    CommodityRequirements,
    DeclarationDownloadRequest,
    DeclarationRequest,
    DeclarationResult,
    DutyInputInferenceRequest,
    DutyInputInferenceResult,
    DutyExplainRequest,
    DutyExplainResponse,
    DutyRequest,
    DutyResult,
    FilingIntentRequest,
    FilingIntentResult,
    HydrationRequest,
    LandedRequest,
    LandedResult,
    ValuationGuideRequest,
    ValuationGuideResult,
    ValuationRequest,
    ValuationResult,
)
from .valuation import calculate_customs_value, choose_valuation_method


# Load .env from project root or backend folder.
for env_path in (
    Path(__file__).parent / ".env",
    Path(__file__).parent.parent / ".env",
    Path(__file__).parent.parent.parent / ".env",
):
    if env_path.exists():
        load_dotenv(env_path)
        break


app = FastAPI(title="AI Fan-Out Trader Journey", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
install_optional_auth(app)

# Best-effort daily OpenAI spend tracker (estimate) for the demo cost banner.
cost.install()


@app.get("/api/cost")
def cost_today():
    return cost.snapshot()


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "auth_enabled": auth_enabled(),
        "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
        "journey_provider_calls_allowed": provider_calls_allowed(),
        "cost": cost.snapshot(),
    }


# --- Classification -----------------------------------------------------

@app.get("/api/journey/examples")
def journey_examples(persona: str | None = None):
    return list_examples(persona=persona)


def _classify_config(config: dict | None) -> dict:
    base = default_classify_config()
    if not config:
        return base
    merged = {**base, **config}
    merged["retrieval"] = {**(base.get("retrieval") or {}), **(config.get("retrieval") or {})}
    merged["kg_include"] = {**(base.get("kg_include") or {}), **(config.get("kg_include") or {})}
    return merged


def _classify_turn(query: str, qa_history: list[dict], config: dict | None, fixed_candidates: list[dict] | None = None, on_progress=None) -> dict:
    cfg = _classify_config(config)
    if cfg.get("strategy") == "eliminate":
        # The eliminate path does not emit progress milestones (item 17 covers
        # the converge path only); the stream still ends with turn + done.
        fixed = fixed_candidates or []
        if not fixed:
            limit = int((cfg.get("retrieval") or {}).get("limit", 40))
            fixed, _ = initial_candidates_for_eliminate(query, cfg, candidate_limit=limit)
        turn = eliminate_step(query, qa_history, fixed, cfg)
        turn["fixed_candidates"] = fixed
        return turn
    turn = classify_step(query=query, qa_history=qa_history, config=cfg, fixed_candidates=fixed_candidates or None, on_progress=on_progress)
    # classify_step now returns its frozen candidate pool; pass it through so the
    # client can echo it back on answer turns (pool freezing across the session).
    turn.setdefault("fixed_candidates", [])
    return turn


@app.post("/api/classify/start", response_model=ClassifyTurn)
def classify_start(req: ClassifyStartRequest) -> ClassifyTurn:
    return ClassifyTurn(**_classify_turn(req.query, [], req.config))


@app.post("/api/classify/answer", response_model=ClassifyTurn)
def classify_answer(req: ClassifyAnswerRequest) -> ClassifyTurn:
    return ClassifyTurn(**_classify_turn(req.query, req.qa_history, req.config, req.fixed_candidates))


# --- SSE streaming variants (backlog item 17) ----------------------------
# Same request models + turn semantics as /api/classify/start|answer (which
# stay untouched as the fallback); the difference is that classification
# milestones are streamed as they happen, then the full turn, then 'done'.

def _sse_message(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _classify_turn_stream(
    query: str,
    qa_history: list[dict],
    config: dict | None,
    fixed_candidates: list[dict] | None,
) -> StreamingResponse:
    """Run _classify_turn in a worker thread, streaming milestone events over SSE.

    The LLM call inside the turn is blocking, so the thread + queue bridge is
    required for events to flush to the client before the turn completes.
    """
    events: queue.Queue = queue.Queue()

    def on_progress(event: str, payload: dict) -> None:
        events.put(("progress", event, payload))

    def worker() -> None:
        try:
            turn = _classify_turn(query, qa_history, config, fixed_candidates, on_progress=on_progress)
            events.put(("turn", ClassifyTurn(**turn).model_dump(mode="json")))
        except Exception as exc:
            events.put(("error", {"detail": f"{type(exc).__name__}: {exc}"}))
        finally:
            events.put(("done",))

    threading.Thread(target=worker, daemon=True).start()

    def event_stream():
        while True:
            try:
                item = events.get(timeout=15.0)
            except queue.Empty:
                yield ": keep-alive\n\n"  # SSE comment - ignored by EventSource
                continue
            if item[0] == "progress":
                yield _sse_message(item[1], item[2])
            elif item[0] == "turn":
                yield _sse_message("turn", item[1])
            elif item[0] == "error":
                yield _sse_message("error", item[1])
            else:  # ("done",)
                yield _sse_message("done", {})
                return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/classify/start/stream")
def classify_start_stream(req: ClassifyStartRequest) -> StreamingResponse:
    return _classify_turn_stream(req.query, [], req.config, None)


@app.post("/api/classify/answer/stream")
def classify_answer_stream(req: ClassifyAnswerRequest) -> StreamingResponse:
    return _classify_turn_stream(req.query, req.qa_history, req.config, req.fixed_candidates)


@app.post("/api/classify/compare")
async def classify_compare(req: dict):
    """Run the same query through N augmentation configs.

    Runs sequentially - parallel execution hit psycopg connection contention
    when 4 panels all opened DB connections + OpenAI calls concurrently.
    Sequential keeps the demo robust at the cost of latency: 4 panels x ~13s = ~50s.
    """
    from fastapi.concurrency import run_in_threadpool
    query = req.get("query", "")
    qa_history = req.get("qa_history", [])
    panels = req.get("panels", [])
    if not query or not panels:
        raise HTTPException(400, "query + panels are required")

    results = []
    for panel in panels:
        cfg = panel.get("config") or {}
        turn = await run_in_threadpool(classify_step, query=query, qa_history=qa_history, config=cfg)
        results.append({"label": panel.get("label", ""), "config": cfg, "turn": turn})
    return {"query": query, "panels": results}


@app.get("/api/db/health")
def db_health():
    return local_db.health()


# --- Valuation ----------------------------------------------------------

@app.post("/api/valuation", response_model=ValuationResult)
def valuation(req: ValuationRequest) -> ValuationResult:
    return calculate_customs_value(req)


@app.post("/api/valuation/guide", response_model=ValuationGuideResult)
def valuation_guide(req: ValuationGuideRequest) -> ValuationGuideResult:
    return choose_valuation_method(req)


# --- Duty ---------------------------------------------------------------

@app.post("/api/duty", response_model=DutyResult)
def duty(req: DutyRequest) -> DutyResult:
    try:
        return calculate_duty(req)
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/duty/requirements/{code}", response_model=CommodityRequirements)
def duty_requirements(code: str) -> CommodityRequirements:
    if commodity_lookup(code) is None:
        raise HTTPException(status_code=404, detail=f"Commodity {code} not found")
    return commodity_requirements(code)


@app.post("/api/duty/explain", response_model=DutyExplainResponse)
def duty_explain(req: DutyExplainRequest) -> DutyExplainResponse:
    payload = req.duty_result.model_dump()
    return DutyExplainResponse(text=explain_duty(payload))


@app.post("/api/duty/infer", response_model=DutyInputInferenceResult)
def duty_infer(req: DutyInputInferenceRequest) -> DutyInputInferenceResult:
    return infer_duty_inputs(req)


@app.get("/api/countries")
def countries():
    return country_list()


@app.get("/api/commodity/{code}")
def commodity(code: str):
    c = commodity_lookup(code)
    if c is None:
        raise HTTPException(status_code=404, detail=f"Commodity {code} not found")
    return c


@app.get("/api/commodity/{code}/hydrate")
def commodity_hydrate(code: str, summarize: bool = False, model: str | None = None):
    payload = hydrate_commodity(code, summarize=summarize, model=model)
    if not payload.get("ok"):
        raise HTTPException(status_code=404, detail=payload.get("error") or f"Commodity {code} not found")
    return payload


@app.post("/api/commodity/{code}/hydrate")
def commodity_hydrate_post(code: str, req: HydrationRequest):
    payload = hydrate_commodity(code, summarize=req.summarize, model=req.model, sources=req.sources)
    if not payload.get("ok"):
        raise HTTPException(status_code=404, detail=payload.get("error") or f"Commodity {code} not found")
    return payload


@app.post("/api/hydration/candidates")
def hydrate_candidates(req: CandidateHydrationRequest):
    candidate_limit = max(1, min(int(req.candidate_limit or 500), 500))
    if req.candidates:
        candidates = req.candidates[:candidate_limit]
    else:
        if not req.query.strip():
            raise HTTPException(status_code=400, detail="query or candidates are required")
        cfg = _classify_config(req.config)
        cfg["use_query_expansion"] = False
        candidates, _ = initial_candidates_for_eliminate(req.query, cfg, candidate_limit=candidate_limit)
    requested_hydrate_limit = int(req.hydrate_limit or 0)
    if requested_hydrate_limit <= 0:
        # The UI sends 0 meaning "all". Hydration costs ~1s+ per candidate
        # (DB reads, optional ATAR fetch/LLM summary), so an uncapped pool of
        # 80+ candidates stalls the request for minutes. Cap the default.
        default_cap = int(os.environ.get("JOURNEY_HYDRATE_DEFAULT_LIMIT", "24"))
        hydrate_limit = min(default_cap, len(candidates))
    else:
        hydrate_limit = min(requested_hydrate_limit, len(candidates))

    to_hydrate = [
        c for c in candidates[:hydrate_limit]
        if c.get("commodity_code") or c.get("code")
    ]
    hydrated = []
    coverage_totals: dict[str, int] = {}
    if to_hydrate:
        with ThreadPoolExecutor(max_workers=min(8, len(to_hydrate))) as pool:
            payloads = list(pool.map(
                lambda c: hydrate_commodity(
                    str(c.get("commodity_code") or c.get("code")),
                    summarize=req.summarize,
                    model=req.model,
                    sources=req.sources,
                ),
                to_hydrate,
            ))
        for candidate, payload in zip(to_hydrate, payloads):
            counts = (payload.get("coverage") or {}).get("counts_by_kind") or {}
            for kind, count in counts.items():
                coverage_totals[kind] = coverage_totals.get(kind, 0) + int(count)
            hydrated.append({"candidate": candidate, "hydration": payload})

    cfg = _classify_config(req.config)
    api_key = None
    question_mode = normalize_candidate_question_mode(req.question_mode)
    provider_question_mode = question_mode in {"facet_rules_llm_wording", "llm_generated"}
    if req.allow_spend and provider_question_mode:
        api_key = load_config().api_keys.openai

    qa_payload = hydrated_candidate_qa(
        candidates,
        hydrated=hydrated,
        qa_history=req.qa_history,
        question_mode=question_mode,
        limit=6,
        cfg=cfg,
        model=cfg.get("question_wording_model") or cfg.get("candidate_selection_model"),
        api_key=api_key,
        allow_provider=bool(req.allow_spend),
    )

    return {
        "query": req.query,
        "summarize": req.summarize,
        "model_requested": req.model,
        "question_mode": question_mode,
        "candidate_count": len(candidates),
        "hydrate_limit": hydrate_limit,
        "cache_write": False,
        "retrieval_guardrail": (
            "This hydrates already-retrieved candidate codes. It does not invent commodity codes. "
            "Writing extracted facets back to kg.commodity_facets is intentionally off in the demo UI."
        ),
        "candidates": candidates,
        "hydrated": hydrated,
        "coverage_totals": coverage_totals,
        "question_hint": qa_payload.get("question_hint"),
        "qa_state": qa_payload.get("qa_state"),
    }


# --- Landed -------------------------------------------------------------

@app.post("/api/landed", response_model=LandedResult)
def landed(req: LandedRequest) -> LandedResult:
    return calculate_landed(req)


# --- Declaration --------------------------------------------------------

@app.post("/api/declaration", response_model=DeclarationResult)
def declaration(req: DeclarationRequest) -> DeclarationResult:
    return build_declaration(req)


@app.post("/api/declaration/file-intent", response_model=FilingIntentResult)
def declaration_file_intent(req: FilingIntentRequest) -> FilingIntentResult:
    ref = f"DECL-{uuid.uuid4().hex[:12].upper()}"
    return FilingIntentResult(
        status="not_submitted",
        reference=ref,
        message="Declaration data is ready for a broker/CDS filing handoff. This app does not submit to HMRC.",
        next_steps=[
            "Review the CDS data elements and supporting documents.",
            "Share the generated JSON with a broker or CDS-connected filing service.",
            "Use the reference for this demo handoff; no live filing has been made.",
        ],
    )


@app.post("/api/declaration/download")
def declaration_download(req: DeclarationDownloadRequest) -> Response:
    code = req.declaration.audit_summary.get("chosen_code") if req.declaration.audit_summary else None
    if not code:
        code = req.declaration.cds_box_values.get("DE 6/14 Commodity code (CN)", "draft")
    safe_code = "".join(ch for ch in str(code) if ch.isalnum()) or "draft"
    body = json.dumps(req.model_dump(mode="json"), indent=2, ensure_ascii=False)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="declaration-{safe_code}.json"'},
    )


# --- KG (educational endpoint) -----------------------------------------

@app.get("/api/kg/edges")
def kg_edges():
    return load_kg_edges()


# --- Q&A loop orchestrator ---------------------------------------------

@app.post("/api/qa/run")
async def qa_run(req: dict):
    """Run the full Q&A loop until commit or max rounds.

    Body:
        query: str (required)
        max_rounds: int = 5
        oracle_text: str | None  (if set, simulator uses LLM with this as ground truth)
        config: classify config dict
        human_answers: list[str] | None  (pre-supplied answers for offline/dev runs)

    Returns the full trace (rounds, qa_history, facts, final mode + candidates).
    """
    from . import qa_loop
    query = (req.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "query is required")
    max_rounds = int(req.get("max_rounds") or 5)
    oracle_text = req.get("oracle_text")
    config = req.get("config")
    human_answers = req.get("human_answers")
    return await qa_loop.run_qa_session(
        query=query,
        max_rounds=max_rounds,
        oracle_text=oracle_text,
        config=config,
        human_answers=human_answers,
    )


# --- Eval harness -------------------------------------------------------

@app.get("/api/eval/runs")
def eval_runs(limit: int = 20):
    """Most recent eval runs with their headline metrics + recall@K curve."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(local_db.DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, run_label, started_at, finished_at, n_queries,
                   recall_at_1, recall_at_5, recall_at_10,
                   recall_at_5_subheading, recall_at_5_heading, mrr,
                   config_json, curve_json, retrieval_limit
            FROM kg.eval_runs
            ORDER BY started_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [
            {k: (float(v) if isinstance(v, (int, float)) and k.startswith(("recall", "mrr")) else v)
             for k, v in dict(r).items()}
            for r in cur.fetchall()
        ]


@app.get("/api/eval/runs/{run_id}/curve")
def eval_run_curve(run_id: int):
    """Full recall@K curve for one run."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(local_db.DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, run_label, retrieval_limit, curve_json, config_json FROM kg.eval_runs WHERE id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "run not found")
    if not row["curve_json"]:
        raise HTTPException(404, "run has no curve_json (pre-refactor row)")
    return dict(row)


@app.get("/api/eval/runs/{run_id}/failures")
def eval_run_failures(run_id: int, persona: str | None = None, limit: int = 100):
    """Per-query results for a run, showing what was returned vs. expected.
    Useful for staring at the failures and understanding where retrieval breaks.
    """
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(local_db.DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        sql = """
          SELECT g.query, g.expected_code, g.expected_description, g.persona, g.source_id,
                 r.top_codes, r.top_sources, r.rank_of_expected, r.rank_subheading, r.rank_heading
          FROM kg.eval_run_results r JOIN kg.eval_gold g ON g.id = r.gold_id
          WHERE r.run_id = %s
        """
        params = [run_id]
        if persona:
            sql += " AND g.persona = %s"
            params.append(persona)
        sql += " ORDER BY (r.rank_of_expected IS NULL) DESC, r.rank_of_expected DESC NULLS FIRST LIMIT %s"
        params.append(limit)
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


@app.get("/api/eval/gold/stats")
def eval_gold_stats():
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(local_db.DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT persona, COUNT(*) AS n FROM kg.eval_gold GROUP BY 1 ORDER BY 1")
        return {"by_persona": [dict(r) for r in cur.fetchall()]}


# --- Experiment matrix page (server-rendered HTML) ----------------------
# Columns = input-quality levels (same ATAR phrased novice -> expert); rows =
# retrieval experiments; cells = recall@K. Plus a drill-down to see one ATAR's
# query across all quality levels. Live from kg.eval_runs + kg.eval_gold.
_PERSONA_ORDER = ["naive_vague", "naive_branded", "naive_specific",
                  "emu_generic", "emu_ordinary", "emu_specific", "original"]
_PERSONA_SHORT = {
    "naive_vague": "naive<br>vague", "naive_branded": "naive<br>branded",
    "naive_specific": "naive<br>specific", "emu_generic": "expert<br>generic",
    "emu_ordinary": "expert<br>ordinary", "emu_specific": "expert<br>specific",
    "original": "ATAR<br>original",
}
_PERSONA_TIPS = {
    "naive_vague": "Novice trader, vague wording - e.g. 'metal machine part'",
    "naive_branded": "Novice trader, brand/everyday wording",
    "naive_specific": "Novice trader, specific wording",
    "emu_generic": "Expert wording but generic - e.g. 'mechanical seal'",
    "emu_ordinary": "Expert wording, ordinary level of detail",
    "emu_specific": "Expert wording, highly specific",
    "original": "The full ATAR product description - the most complete possible input",
}
# (_ROW_ORDER removed - the matrix now renders ALL valid configs in one ranked table,
#  not a fixed ladder. _ROW_LABELS / _ROW_TIPS below are still used for plain-English labels.)
_ROW_LABELS = {
    "baseline_fts_only": "Keyword search (legacy, non-AI)",
    "ai_semantic": "AI semantic search (v1 base)",
    "ai_semantic_composite": "+ contextualised code descriptions",
    "ai_semantic_triage": "+ query rewrite (trader words -> tariff terms)",
    "ai_semantic_composite_triage": "exp: semantic + AI-enriched text + query rewrite (gpt-5-mini, no Search References)",
    "v2_plus_desc_vec": "exp: AI semantic + KG + Facets",
    "ai_kg": "exp: AI semantic + Knowledge Graph",
    "ai_facets": "exp: AI semantic + Facets",
    "production_v2": "exp: KG + Facets only, no semantic desc (misnamed config)",
    "v2_plus_multi_query": "exp: AI semantic + KG/Facets + query rephrasing",
    "v2_plus_both": "exp: AI semantic + KG/Facets + rephrasing",
    "v2_plus_descvec_adapter": "exp: + trained embedding tweak (dead end)",
    "rescue_descvec": "exp: + LLM rescues deeper results (mixed)",
    "hyde_descvec": "exp: + AI drafts a tariff-style description (HyDE)",
    "v2_composite": "exp: + contextualised code descriptions (a prod feature)",
    "v2_triage": "exp: + tariff-vocab query rewrite (a prod feature)",
    "v2_composite_triage": "exp: + contextualised descriptions + query rewrite (both)",
    "facts_cap03": "exp: AI-enriched text + rewrite + facts, capped 0.3",
    "facts_cap05": "exp: AI-enriched text + rewrite + facts, capped 0.5",
    "facts_cap07": "exp: AI-enriched text + rewrite + facts, capped 0.7",
    "staging_ai": "OTT staging leg-set, rewrite OFF (no-rewrite floor)",
    "rw_g5_mine": "staging legs + rewrite (gpt-5-mini, eval prompt)",
    "rw_g41_mine": "staging legs + rewrite (gpt-4.1-mini, eval prompt)",
    "rw_g5_staging": "staging legs + rewrite (gpt-5-mini, staging prompt)",
    "rw_g41_staging": "= faithful STAGING rewrite (gpt-4.1-mini + staging prompt)",
}
_ROW_TIPS = {
    "baseline_fts_only": "Old-style search: matches the literal words the trader types against tariff descriptions. No AI.",
    "ai_semantic": "Embeds the query and tariff descriptions as vectors and matches by meaning. The v1 semantic base - NO KG, NO facets, NO AI-enriched code text, NO query rewrite.",
    "ai_semantic_composite": "Semantic search over AI-enriched code text (self-text + synonyms + colloquial terms + brands). No query rewrite. Isolates the document-side production feature. (Internal ref: AI-166.)",
    "ai_semantic_triage": "Semantic search with the query rewrite turned ON (trader words -> tariff vocabulary before searching). No AI-enriched code text. Isolates the query-side production feature. (Internal ref: AI-815.)",
    "ai_semantic_composite_triage": "Semantic + AI-enriched code text + query rewrite (gpt-5-mini), NO KG/facets/Search References. Staging DOES run rewrite (gpt-4.1-mini, conditional) - this is the same shape on a stronger rewrite model, minus Search References. (Internal refs: AI-166 text enrichment, AI-815 rewrite.)",
    "ai_kg": "EXPERIMENTAL (not shipped): AI semantic search plus the rules layer - chapter/section notes, ATAR rulings, HSEN.",
    "ai_facets": "EXPERIMENTAL (not shipped): AI semantic search plus structured per-commodity facts (material, intended use, form...).",
    "production_v2": "EXPERIMENTAL config with a misleading name - it is Knowledge Graph + Facets with NO semantic descriptions. This is NOT the shipped search.",
    "v2_plus_desc_vec": "EXPERIMENTAL: AI semantic descriptions plus the KG + Facets layer. The fullest experimental retrieval stack.",
    "v2_plus_multi_query": "Before searching, an LLM rewrites the query into 3-5 alternative wordings and merges the results - catches products one phrasing would miss.",
    "v2_plus_both": "Semantic descriptions plus the multi-wording rephrasing, combined.",
    "v2_plus_descvec_adapter": "A small trained adjustment to query embeddings to align trader words with tariff words. Tested and dropped - no gain.",
    "rescue_descvec": "After retrieval, an LLM promotes promising candidates from ranks 80-200 into the top 100. Helps some products, hurts others.",
    "hyde_descvec": "The AI drafts a hypothetical tariff-style product description from the query and searches with that, bridging trader vs tariff language (HyDE).",
    "v2_composite": "Searches the contextualised descriptions + synonyms + colloquial terms + brands, not just raw self-text - so catch-all 'Other' codes become findable. (Internal ref: AI-166.)",
    "v2_triage": "A small LLM rewrites the trader's plain words into formal tariff vocabulary before searching (production 'triage' feature; internal ref AI-815).",
    "v2_composite_triage": "Both levers together: contextualised code descriptions (document side) + tariff-vocabulary query rewrite (query side).",
    "staging_ai": "The OTT staging leg-set (AI-enriched code text + Search References + semantic + RRF) with query rewrite OFF - the no-rewrite floor. Real staging runs rewrite ON, so this UNDER-states deployed staging; see the pinned 'AI search (OTT staging)' row.",
    "rw_g41_staging": "FAITHFUL OTT staging rewrite: gpt-4.1-mini + staging's exact expand_query_context prompt, on the staging leg-set. CAVEAT: staging rewrites only WHEN-NEEDED (conditional); this rewrites EVERY query, so it is an upper bound - true staging recall sits between this (~77%) and the no-rewrite floor (~70%).",
}
_PRED_LABELS = {
    "avg_ictf": "word rarity (corpus)", "avg_idf": "word rarity (avg)", "max_idf": "word rarity (rarest)",
    "llm_spec": "LLM specificity", "nn_density": "neighbourhood density", "scs": "clarity score",
    "query_len": "query length",
}

# Full-factorial grid (run_eval._grid_configs): 4 binary axes on the semantic base.
# C=AI-enriched code text (AI-166), F=facts (FTS+vec), K=KG (FTS+vec), T=triage (query rewrite).
_GRID_AXES = [("C", "AI-enriched text"), ("F", "facts"), ("K", "KG"), ("T", "triage")]


def _grid_label(run_label: str) -> str:
    """grid_C1F0K1T0 -> human label listing the ON axes."""
    bits = run_label[len("grid_"):] if run_label.startswith("grid_") else run_label
    on = [name for (letter, name), c in zip(_GRID_AXES, [bits[i + 1] for i in range(0, len(bits), 2)]) if c == "1"]
    return "semantic base + " + " + ".join(on) if on else "semantic base only"


def _grid_tip(run_label: str) -> str:
    bits = run_label[len("grid_"):] if run_label.startswith("grid_") else run_label
    state = {letter: bits[i + 1] for i, (letter, _) in zip(range(0, len(bits), 2), _GRID_AXES)}
    parts = [f"{name} {'ON' if state.get(letter) == '1' else 'off'}" for letter, name in _GRID_AXES]
    return ("Full-factorial grid cell. Base = AI semantic vector + Search References, LOO-honest. "
            "Axes: " + ", ".join(parts) + ". (AI-enriched text=AI-166 contextualised docs; triage=AI-815 query rewrite.)")


# ---- Generic label/tooltip from config_json (for configs with no curated label) ----
# The ON legs of a config, in reading order. `use_vector` ON is the AI semantic base.
_CFG_LEGS = [
    ("use_vector", "semantic"),
    ("use_composite", "AI-enriched text"),
    ("use_kg_context", "KG"),
    ("use_facts", "facets"),
    ("triage", "triage"),
    ("use_curated", "Search References"),
]


def _cfg_label(cfg: dict) -> str:
    """Readable label from config flags, e.g. 'semantic + facts + KG + triage'."""
    cfg = cfg or {}
    on = [name for key, name in _CFG_LEGS if cfg.get(key)]
    if not on:
        return "keyword (no semantic)"
    return " + ".join(on)


def _cfg_tip(cfg: dict) -> str:
    """Technical detail tooltip: the raw ON/off state of each retrieval leg."""
    cfg = cfg or {}
    parts = [f"{name}={'ON' if cfg.get(key) else 'off'}" for key, name in _CFG_LEGS]
    parts.append(f"loo={'ON' if cfg.get('loo') else 'off'}")
    return "Config legs: " + ", ".join(parts) + "."


def _matrix_cell(v):
    if v is None:
        return '<td style="background:#1f2937;color:#6b7280;text-align:center">-</td>'
    if v >= 0.9: bg, fg = "#064e3b", "#6ee7b7"
    elif v >= 0.8: bg, fg = "#065f46", "#a7f3d0"
    elif v >= 0.7: bg, fg = "#3f6212", "#d9f99d"
    elif v >= 0.6: bg, fg = "#854d0e", "#fde68a"
    elif v >= 0.4: bg, fg = "#7c2d12", "#fed7aa"
    else: bg, fg = "#7f1d1d", "#fecaca"
    return f'<td style="background:{bg};color:{fg};text-align:center;font-variant-numeric:tabular-nums">{v * 100:.1f}%</td>'


def _iq_cell(v):
    """0-100 lexical-specificity cell (green = more specific-looking wording, red = vaguer). NOT a recall predictor (r~0.13)."""
    if v is None:
        return '<td style="text-align:center;color:#6b7280">-</td>'
    v = float(v)
    if v >= 70: bg, fg = "#064e3b", "#6ee7b7"
    elif v >= 55: bg, fg = "#065f46", "#a7f3d0"
    elif v >= 45: bg, fg = "#3f6212", "#d9f99d"
    elif v >= 35: bg, fg = "#854d0e", "#fde68a"
    else: bg, fg = "#7c2d12", "#fed7aa"
    return f'<td style="background:{bg};color:{fg};text-align:center;font-variant-numeric:tabular-nums">{v:.0f}</td>'


# ---- Code-macro recall (the honest headline) --------------------------------
# Plain row-recall over-weights commodity codes that have MANY gold rows (multi-
# ATAR codes, dupes). The headline is therefore a code-persona MACRO:
#   per-persona column  = avg over expected_code of (hit-rate for code within persona)
#   headline (overall)  = avg over expected_code of [avg over persona of per-(code,persona) hit-rate]
# Computed from rank_of_expected (the true rank over the full candidate list, so
# valid for any k up to the run's retrieval_limit), restricted to active gold.
def _code_macro_recall(conn, run_ids: list[int], k: int) -> dict[int, dict]:
    """Return {run_id: {persona: macro_recall, "__overall__": headline_macro}}.

    One query for all runs. `persona='__overall__'` carries the code-persona macro
    (mean over codes of the per-code mean-over-persona hit-rate)."""
    if not run_ids:
        return {}
    # Tolerate the `active` column being absent (pre-migration DB).
    active_join = "AND g.active"
    try:
        with conn.cursor() as probe:
            probe.execute("SELECT active FROM kg.eval_gold LIMIT 1")
    except Exception:
        conn.rollback()
        active_join = ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH cell AS (   -- hit-rate per (run, persona, code)
              SELECT rr.run_id, g.persona, g.expected_code,
                     avg((rr.rank_of_expected IS NOT NULL AND rr.rank_of_expected <= %s)::int) AS hr
              FROM kg.eval_run_results rr
              JOIN kg.eval_gold g ON g.id = rr.gold_id
              WHERE rr.run_id = ANY(%s) {active_join}
              GROUP BY rr.run_id, g.persona, g.expected_code
            ),
            per_persona AS (   -- macro over codes, within each persona
              SELECT run_id, persona, avg(hr) AS macro FROM cell GROUP BY run_id, persona
            ),
            per_code AS (      -- per code: mean over personas (for the overall headline)
              SELECT run_id, expected_code, avg(hr) AS code_mean FROM cell GROUP BY run_id, expected_code
            ),
            overall AS (
              SELECT run_id, avg(code_mean) AS macro FROM per_code GROUP BY run_id
            )
            SELECT run_id, persona, macro::float8 AS macro FROM per_persona
            UNION ALL
            SELECT run_id, '__overall__' AS persona, macro::float8 AS macro FROM overall
            """,
            (k, run_ids),
        )
        out: dict[int, dict] = {}
        for r in cur.fetchall():
            out.setdefault(r["run_id"], {})[r["persona"]] = r["macro"]
        return out


@app.get("/eval/matrix", response_class=HTMLResponse)
def eval_matrix(k: int = 100):
    import json as _json
    import psycopg
    from psycopg.rows import dict_row
    kk = str(k)
    with psycopg.connect(local_db.DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (run_label) run_label, id, started_at, config_json,
                   curve_json->'per_persona' AS pp
            FROM kg.eval_runs er
            WHERE curve_json ? 'per_persona' AND finished_at IS NOT NULL
              -- valid-run filter: drop 0-result / mismatched runs (n_queries must
              -- equal the rows actually written to eval_run_results).
              AND n_queries = (SELECT count(*) FROM kg.eval_run_results WHERE run_id = er.id)
              -- drop tiny smoke / scratch runs (not real configs).
              AND run_label NOT LIKE 'smoke%%' AND run_label NOT LIKE '%%_test'
              AND run_label NOT LIKE '%%_test_%%'
            ORDER BY run_label,
                     (SELECT count(*) FROM jsonb_object_keys(curve_json->'per_persona')) DESC,
                     started_at DESC
            """
        )
        runs = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """
            SELECT source_id, persona, query, expected_code, expected_description
            FROM kg.eval_gold
            WHERE source_id IN (
                SELECT source_id FROM kg.eval_gold WHERE persona='naive_vague' ORDER BY id LIMIT 150
            )
            ORDER BY source_id, persona
            """
        )
        gold = [dict(r) for r in cur.fetchall()]

    # per-persona descriptiveness (avg words + LLM spec /10), if scored yet
    desc = {}
    try:
        with psycopg.connect(local_db.DSN, row_factory=dict_row) as c2, c2.cursor() as cur2:
            cur2.execute("SELECT persona, round(avg(n_words),1) w, round(avg(llm_spec),2) s "
                         "FROM kg.query_descriptiveness GROUP BY persona")
            desc = {r["persona"]: dict(r) for r in cur2.fetchall()}
    except Exception:
        desc = {}

    # per-persona QPP specificity predictors (avgIDF/maxIDF/avgICTF/SCS/nn_density)
    qpp = {}
    try:
        with psycopg.connect(local_db.DSN, row_factory=dict_row) as c3, c3.cursor() as cur3:
            cur3.execute("SELECT persona, round(avg(input_quality),0) iq, round(avg(query_len),1) ql, "
                         "round(avg(avg_idf),2) idf, round(avg(max_idf),2) midf, round(avg(avg_ictf),2) ictf, "
                         "round(avg(scs),2) scs, round(avg(nn_density),3) nnd FROM kg.query_qpp GROUP BY persona")
            qpp = {r["persona"]: dict(r) for r in cur3.fetchall()}
    except Exception:
        qpp = {}

    # predictor power: point-biserial r of each signal vs gold-in-top-100 (desc_vec run)
    import numpy as _np
    corr = []
    try:
        with psycopg.connect(local_db.DSN, row_factory=dict_row) as c4, c4.cursor() as cur4:
            cur4.execute(
                "SELECT q.query_len, q.avg_idf, q.max_idf, q.avg_ictf, q.scs, q.nn_density, d.llm_spec, "
                "(rr.rank_of_expected IS NOT NULL AND rr.rank_of_expected<=100)::int hit "
                "FROM kg.query_qpp q JOIN kg.eval_run_results rr ON rr.gold_id=q.gold_id "
                "JOIN kg.eval_runs er ON er.id=rr.run_id "
                "LEFT JOIN kg.query_descriptiveness d ON d.gold_id=q.gold_id "
                "WHERE er.id=(SELECT max(id) FROM kg.eval_runs WHERE run_label='v2_plus_desc_vec' "
                "AND finished_at IS NOT NULL AND curve_json IS NOT NULL)")
            crows = [dict(r) for r in cur4.fetchall()]
        if crows:
            hit = _np.array([r["hit"] for r in crows], dtype=float)
            for pred in ["avg_ictf", "avg_idf", "max_idf", "llm_spec", "nn_density", "scs", "query_len"]:
                vals = _np.array([float(r[pred]) if r[pred] is not None else _np.nan for r in crows])
                m = ~_np.isnan(vals)
                if m.sum() > 2 and _np.std(vals[m]) > 0:
                    corr.append((pred, float(_np.corrcoef(vals[m], hit[m])[0, 1])))
            corr.sort(key=lambda x: -abs(x[1]))
    except Exception:
        corr = []
    if corr:
        _cr = "".join(
            "<tr><td style='padding:4px 14px'>" + _PRED_LABELS.get(p, p) + "</td>"
            "<td style='padding:4px 14px;text-align:right;font-variant-numeric:tabular-nums;color:"
            + ("#6ee7b7" if abs(r) >= 0.2 else "#fde68a" if abs(r) >= 0.1 else "#9ca3af") + "'>"
            + f"{r:+.3f}</td></tr>" for p, r in corr)
        corr_panel = ("<div class='axis' style='margin-top:30px'>which input signal predicts recall@100? "
                      "(point-biserial r vs gold-in-top-100, desc_vec run; weak across the board = pipeline is phrasing-robust)</div>"
                      "<table style='width:auto'><thead><tr><th style='text-align:left'>predictor</th>"
                      "<th style='text-align:right'>r</th></tr></thead><tbody>" + _cr + "</tbody></table>")
    else:
        corr_panel = ""

    header_cells = "".join(
        f'<th title="{_PERSONA_TIPS.get(p, "")}" style="text-align:center;font-size:11px;line-height:1.2">{_PERSONA_SHORT.get(p, p)}</th>'
        for p in _PERSONA_ORDER
    )

    # ---- Code-macro recall (the honest headline) for EVERY valid config. ------
    # macro[run][persona] is the per-persona code-macro; macro[run]['__overall__']
    # is the code-persona headline. This replaces the row-level
    # curve_json->per_persona (which over-weights multi-ATAR / duplicate codes).
    _all_run_ids = [r["id"] for r in runs]
    with psycopg.connect(local_db.DSN, row_factory=dict_row) as cm_conn:
        macro = _code_macro_recall(cm_conn, _all_run_ids, k)

    def _macro_val(run_id, persona):
        return (macro.get(run_id) or {}).get(persona)

    # Pre-LOO-fix cutoff: runs that started before this used the leaky resolver and
    # haven't been re-run on the clean harness yet. Mark them stale (they'll refresh
    # as the clean re-runs land).
    import datetime as _dt
    _LOO_FIX_AT = _dt.datetime(2026, 6, 3, 17, 25)

    def _is_stale(r) -> bool:
        sa = r.get("started_at")
        if sa is None:
            return False
        if sa.tzinfo is not None:
            sa = sa.replace(tzinfo=None)
        return sa < _LOO_FIX_AT

    def _label_of(r) -> str:
        """Plain-English label: curated label if we have one, else grid axes, else
        derived from config flags."""
        rl = r["run_label"]
        if rl in _ROW_LABELS:
            return _ROW_LABELS[rl]
        if rl.startswith("grid_"):
            return _grid_label(rl)
        return _cfg_label(r.get("config_json"))

    def _tip_of(r) -> str:
        rl = r["run_label"]
        if rl in _ROW_TIPS:
            return _ROW_TIPS[rl]
        if rl.startswith("grid_"):
            return _grid_tip(rl)
        return _cfg_tip(r.get("config_json"))

    _overall_th = ("<th title='Code-persona MACRO: mean over the 116 commodity codes of each "
                   "code&#39;s mean-over-persona hit-rate. The headline - not skewed by codes "
                   "with many gold rows.' style='text-align:center;font-size:11px;line-height:1.2;"
                   "background:#0d1f17'>code-macro<br>(headline)</th>")
    _stale_badge = ("<span title='Latest run for this config predates the LOO-leakage fix "
                    "(2026-06-03 17:25); recall may be inflated. Will refresh when the clean "
                    "re-run lands.' style='display:inline-block;margin-left:8px;padding:0 5px;"
                    "border-radius:4px;background:#3f2d0e;color:#fbbf24;font-size:9px;"
                    "vertical-align:middle'>stale</span>")

    def _row_html(r, rank=None, label=None, badge=None):
        overall = _macro_val(r["id"], "__overall__")
        cells = _matrix_cell(overall) + "".join(_matrix_cell(_macro_val(r["id"], p)) for p in _PERSONA_ORDER)
        lbl = label if label is not None else _label_of(r)
        tip = _tip_of(r)
        stale = _stale_badge if _is_stale(r) else ""
        rank_td = (f'<td style="text-align:right;color:#6b7280;font-variant-numeric:tabular-nums">{rank}</td>'
                   if rank is not None else
                   '<td style="text-align:right;color:#6b7280">&middot;</td>')
        return (rank_td +
                f'<td class="rl" title="{tip}">{badge or ""}<span class="lbl">{lbl}</span>{stale}'
                f'<span class="key">{r["run_label"]}</span></td>' + cells + "</tr>")

    # ---- Pin the 2 OTT baselines at the very top (visually distinct), then a SINGLE
    # ranked body of every other config by overall code-macro recall@k descending.
    by_label = {r["run_label"]: r for r in runs}
    classic = by_label.get("baseline_fts_only")
    # Real OTT staging HAS query rewrite (ExpandSearchQueryService on, conditional). Pin the
    # faithful staging config (gpt-4.1-mini + staging's expand_query_context prompt). staging_ai
    # (rewrite OFF) falls into the ranking below as the no-rewrite floor.
    ai = (by_label.get("rw_g41_staging") or by_label.get("staging_ai")
          or by_label.get("ai_semantic_composite"))
    pinned_ids = {id(x) for x in (classic, ai) if x}

    _ott_badge = ("<span style='display:inline-block;margin-right:8px;padding:0 6px;border-radius:4px;"
                  "background:#0d2b4e;color:#7dd3fc;font-size:9px;font-weight:700;letter-spacing:.5px;"
                  "vertical-align:middle'>OTT BASELINE</span>")
    pin_rows = []
    if classic:
        pin_rows.append("<tr style='background:#0d1424'>"
                        + _row_html(classic, rank=None, label="Classic keyword search (OTT live)", badge=_ott_badge))
    if ai:
        pin_rows.append("<tr style='background:#0d1424'>"
                        + _row_html(ai, rank=None, label="AI search (OTT staging)", badge=_ott_badge))

    # ONE unified ranking: every config (LOO on or off) ranked together by code-macro
    # recall@k. LOO on/off is a per-config leg shown in the hover tooltip, not a basis
    # for segregating the table.
    ranked = [r for r in runs if id(r) not in pinned_ids]
    ranked.sort(key=lambda r: (_macro_val(r["id"], "__overall__") is None,
                               -(_macro_val(r["id"], "__overall__") or 0.0)))
    ranked_rows = ["<tr>" + _row_html(r, rank=rank) for rank, r in enumerate(ranked, start=1)]

    _colspan = str(2 + 1 + len(_PERSONA_ORDER))
    divider = ("<tr><td colspan='" + _colspan + "' "
               "style='background:#070b14;color:#6b7280;font-size:10px;letter-spacing:1px;"
               "text-transform:uppercase;padding:5px 10px'>ranked experiments &mdash; "
               f"code-macro recall@{k}, best first</td></tr>")

    ranked_panel = (
        f"<div class='axis' style='margin-top:8px'>all retrieval configs &mdash; one ranked table, "
        f"code-macro recall@{k} (best first)</div>"
        "<div class='hint'>Every config with a finished, valid run, ranked by the headline "
        "code-macro. The two <b>OTT BASELINE</b> rows (classic keyword, AI staging) are pinned on "
        "top for reference. Cells = <b>code-persona macro</b> (per-code hit-rate averaged over "
        "codes), so multi-ATAR / duplicate codes no longer inflate the score. A <b>stale</b> tag "
        "means that config's latest run predates the LOO fix and will refresh as clean re-runs "
        "land. Per-rank detail (recall@1 / @10) lives in the eval tab. Hover any label for the "
        "technical config legs (incl. LOO on/off); grey text is the config name.</div>"
        "<table><thead><tr><th style='text-align:right;width:30px'>#</th>"
        "<th style='text-align:left'>setup</th>" + _overall_th + header_cells + "</tr></thead><tbody>"
        + "".join(pin_rows) + divider + "".join(ranked_rows) + "</tbody></table>"
    )

    def _signal_row(label, tip, src, key, fmt):
        cells = []
        for p in _PERSONA_ORDER:
            d = src.get(p)
            v = d.get(key) if d else None
            txt = ("{:" + fmt + "}").format(float(v)) if v is not None else "-"
            cells.append(f'<td style="text-align:center;color:#a5b4fc;font-size:11px;font-style:italic">{txt}</td>')
        return ('<tr class="qpp" style="display:none;background:#0d1424"><td class="rl" title="' + tip
                + '" style="color:#818cf8;font-size:11px;font-style:italic">' + label + "</td>"
                + "".join(cells) + "</tr>")

    # input-quality signal rows (per-persona means) - the literature QPP set + the LLM score
    signal_rows = (
        _signal_row("query length", "Average meaningful words in the query (stopwords removed). Longer usually = more descriptive.", qpp, "ql", ".1f")
        + _signal_row("word rarity (avg)", "How rare the query's words are across tariff text - higher = more specific/distinctive wording. [avgIDF]", qpp, "idf", ".2f")
        + _signal_row("word rarity (rarest)", "Rarity of the single most distinctive word in the query. [maxIDF]", qpp, "midf", ".2f")
        + _signal_row("word rarity (corpus)", "Specificity by total word frequency across the corpus. [avgICTF]", qpp, "ictf", ".2f")
        + _signal_row("clarity score", "Query length + word rarity combined into one specificity number (Simplified Clarity Score).", qpp, "scs", ".2f")
        + _signal_row("neighbourhood density", "How tightly the query's nearest tariff descriptions cluster - high = a crowded, confusable region (harder).", qpp, "nnd", ".3f")
        + _signal_row("LLM specificity /10", "An LLM's 0-10 rating of how completely the query pins down a single product.", desc, "s", ".1f")
    )

    examples = {}
    for g in gold:
        ex = examples.setdefault(g["source_id"], {"code": g["expected_code"],
                                                  "desc": (g["expected_description"] or "")[:160], "queries": {}})
        ex["queries"][g["persona"]] = g["query"]
    options = "".join(f'<option value="{sid}">{sid} -&gt; {ex["code"]}</option>' for sid, ex in examples.items())

    css = (
        "body{margin:0;background:#0b0f19;color:#e5e7eb;font-family:Inter,system-ui,sans-serif;padding:28px 36px}"
        "h1{font-size:24px;margin:0 0 4px}.sub{color:#9ca3af;font-size:13px;margin-bottom:22px;max-width:920px;line-height:1.5}"
        "table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:8px}"
        "th,td{border:1px solid #1f2937;padding:7px 10px}thead th{background:#111827;color:#cbd5e1;font-weight:600}"
        ".axis{font-size:11px;color:#6b7280;letter-spacing:1px;text-transform:uppercase;margin:18px 0 6px}"
        "select{background:#111827;color:#e5e7eb;border:1px solid #374151;border-radius:6px;padding:8px 10px;font-size:14px;min-width:420px}"
        ".card{background:#0f1629;border:1px solid #1f2937;border-radius:10px;padding:18px;margin-top:14px}"
        ".lvl{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid #1f2937}.lvl:last-child{border-bottom:none}"
        ".lvltag{flex:0 0 130px;font-family:monospace;font-size:11px;color:#7dd3fc}.lvlq{font-size:14px}"
        "td.rl{text-align:left;white-space:nowrap;cursor:help}.lbl{color:#cbd5e1}"
        ".key{display:block;font-family:monospace;font-size:9.5px;color:#5b6677;margin-top:1px}"
        "thead th{cursor:help}.hint{color:#5b6677;font-size:11px;margin:2px 0 14px}"
    )
    js = (
        "const EX=__EX__;const PORDER=__PORDER__;"
        "const L={naive_vague:'naive / vague',naive_branded:'naive / branded',naive_specific:'naive / specific',"
        "emu_generic:'expert / generic',emu_ordinary:'expert / ordinary',emu_specific:'expert / specific',original:'ATAR original'};"
        "function show(s){const e=EX[s];if(!e)return;let h='<div style=\"color:#9ca3af;font-size:12px;margin-bottom:10px\">Gold <b style=\"color:#6ee7b7;font-family:monospace\">'+e.code+'</b> &middot; '+e.desc+'</div>';"
        "for(const p of PORDER){const q=e.queries[p];if(!q)continue;h+='<div class=\"lvl\"><div class=\"lvltag\">'+(L[p]||p)+'</div><div class=\"lvlq\">\"'+q+'\"</div></div>';}"
        "document.getElementById('grad').innerHTML=h;}"
        "document.getElementById('sel').addEventListener('change',e=>show(e.target.value));"
        "window.addEventListener('DOMContentLoaded',()=>{const f=document.getElementById('sel');if(f.value)show(f.value);});"
        "function toggleQpp(){var rows=document.querySelectorAll('tr.qpp');if(!rows.length)return;"
        "var hidden=rows[0].style.display==='none';for(var i=0;i<rows.length;i++){rows[i].style.display=hidden?'table-row':'none';}"
        "document.getElementById('qpp-hd').innerHTML=(hidden?'&#9662;':'&#9656;')+' Lexical specificity (0-100) &mdash; click to '+(hidden?'collapse the signals':'expand for the signals');}"
    ).replace("__EX__", _json.dumps(examples)).replace("__PORDER__", _json.dumps(_PERSONA_ORDER))

    # Phrasing-context strip (per-persona lexical specificity + its signals).
    # Standalone table - it describes the COLUMNS (input phrasing), not any run.
    lexical_table = (
        "<div class='axis' style='margin-top:8px'>input phrasing context (per column) &mdash; "
        "lexical specificity, not a recall predictor</div>"
        "<table><thead><tr><th style='text-align:left'>phrasing signal</th>" + header_cells
        + "</tr></thead><tbody>"
        + ("<tr style='background:#0d1424'><td class='rl' id='qpp-hd' onclick='toggleQpp()' "
           "title='LEXICAL SPECIFICITY (0-100): a blend of how rare/specific the WORDING looks, from the signals below "
           "(query length + word rarity + LLM specificity, minus neighbourhood density; SCS excluded). "
           "This measures phrasing, NOT retrievability - it correlates only weakly with recall (r~0.13) and brand/novel terms inflate it. Click to expand the signals.' "
           "style='cursor:pointer;color:#cbd5e1'>&#9656; Lexical specificity (0-100) &mdash; measures wording, not recall; click for the signals</td>"
           + "".join(_iq_cell((qpp.get(p) or {}).get("iq")) for p in _PERSONA_ORDER) + "</tr>")
        + signal_rows + "</tbody></table>"
    )

    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Experiment matrix</title><style>" + css + "</style></head><body>"
        "<h1>Retrieval experiment matrix</h1>"
        f"<div class='sub'>Headline metric = <b>code-persona macro recall@{k}</b>: for each of the "
        "<b>116 commodity codes</b> we take the share of its gold queries whose correct code lands "
        f"in the top {k}, average that over the 7 phrasings, then average over codes. Macro-averaging "
        "stops codes with many gold rows (multi-ATAR products, duplicates) from dominating the score. "
        "Each ATAR is phrased 7 ways, novice/vague to expert/ATAR-original (the columns). "
        "All runs are LOO-honest (the queried code's own ATAR fingerprints are excluded), use active "
        "(de-duplicated) gold, and exclude 0-result / mismatched runs.</div>"
        "<div class='hint'>One ranked table of <b>every</b> retrieval config with a finished, valid "
        "run, best first by the headline code-macro. The two <b>OTT BASELINE</b> rows (classic keyword "
        "and AI staging) are pinned on top for reference. Hover any label for the technical config legs; "
        "grey text is the internal config name. A <b>stale</b> tag flags configs whose latest run "
        "predates the LOO-leakage fix.</div>"
        "<div class='axis'>&larr; same item, 7 phrasings: novice / vague &nbsp;...&nbsp; expert / ATAR-original &rarr;</div>"
        + ranked_panel
        + lexical_table
        + corr_panel
        + "<div class='axis' style='margin-top:30px'>see the same item phrased 7 ways (novice -&gt; expert)</div>"
        "<select id='sel'>" + options + "</select><div class='card' id='grad'></div>"
        "<script>" + js + "</script></body></html>"
    )


# --- Classification matrix page (server-rendered HTML) ------------------
# The disambiguation analogue of /eval/matrix. Columns = personas (input
# quality), rows = config (strategy x prompt_mode x augmentation x model),
# cells = gold-in-final-SET % (PRESENCE - the primary metric). Hover a cell for
# rank / survivor-set size / rounds / est $/session. Live from kg.classify_runs.
@app.get("/eval/classify-matrix", response_class=HTMLResponse)
def eval_classify_matrix():
    import psycopg
    from psycopg.rows import dict_row

    porder = _PERSONA_ORDER  # reuse the retrieval-matrix persona ordering + labels
    rows_by_label: dict[str, dict] = {}
    have_table = True
    try:
        with psycopg.connect(local_db.DSN, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT run_label, persona,
                       count(*) AS n,
                       avg(gold_in_final_set::int) AS in_set,
                       avg(gold_in_top1::int)      AS top1,
                       avg(gold_in_top5::int)      AS top5,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY gold_rank)
                         FILTER (WHERE gold_rank IS NOT NULL) AS med_rank,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY survivor_set_size) AS med_size,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY rounds) AS med_rounds,
                       avg(est_cost_usd) AS avg_cost,
                       max(strategy) AS strategy, max(prompt_mode) AS prompt_mode,
                       max(augmentation) AS augmentation, max(model) AS model
                FROM kg.classify_runs
                GROUP BY run_label, persona
                """
            )
            agg = [dict(r) for r in cur.fetchall()]
    except Exception as exc:
        have_table = False
        agg = []
        _err = str(exc)

    meta: dict[str, dict] = {}
    for r in agg:
        lbl = r["run_label"]
        d = rows_by_label.setdefault(lbl, {})
        d[r["persona"]] = r
        meta.setdefault(lbl, {
            "strategy": r["strategy"], "prompt_mode": r["prompt_mode"],
            "augmentation": r["augmentation"], "model": r["model"],
        })
    # Order rows: eliminate before converge (the new lever first), then by label.
    labels = sorted(rows_by_label, key=lambda l: (meta[l]["strategy"] != "eliminate", l))

    def _cell(r):
        if not r:
            return '<td style="background:#1f2937;color:#6b7280;text-align:center">-</td>'
        v = float(r["in_set"] or 0)
        if v >= 0.9: bg, fg = "#064e3b", "#6ee7b7"
        elif v >= 0.8: bg, fg = "#065f46", "#a7f3d0"
        elif v >= 0.7: bg, fg = "#3f6212", "#d9f99d"
        elif v >= 0.6: bg, fg = "#854d0e", "#fde68a"
        elif v >= 0.4: bg, fg = "#7c2d12", "#fed7aa"
        else: bg, fg = "#7f1d1d", "#fecaca"
        rank = r["med_rank"]
        tip = (
            f"n={r['n']} | gold-in-set {v*100:.0f}% | top1 {float(r['top1'] or 0)*100:.0f}% "
            f"| top5 {float(r['top5'] or 0)*100:.0f}% | med rank "
            f"{('%.0f' % rank) if rank is not None else '-'} "
            f"| med survivors {float(r['med_size'] or 0):.0f} | med rounds "
            f"{float(r['med_rounds'] or 0):.1f} | est ${float(r['avg_cost'] or 0):.4f}/sess"
        )
        return (f'<td title="{tip}" style="background:{bg};color:{fg};text-align:center;'
                f'font-variant-numeric:tabular-nums">{v*100:.0f}%</td>')

    header_cells = "".join(
        f'<th title="{_PERSONA_TIPS.get(p, "")}" style="text-align:center;font-size:11px;'
        f'line-height:1.2">{_PERSONA_SHORT.get(p, p)}</th>'
        for p in porder
    )
    body_rows = []
    for lbl in labels:
        m = meta[lbl]
        prow = rows_by_label[lbl]
        cells = "".join(_cell(prow.get(p)) for p in porder)
        strat_badge = ("#6ee7b7" if m["strategy"] == "eliminate" else "#93c5fd")
        body_rows.append(
            f'<tr><td class="rl"><span class="lbl" style="color:{strat_badge}">'
            f'{m["strategy"]}</span> &middot; {m["prompt_mode"]} &middot; {m["augmentation"]}'
            f'<span class="key">{lbl} ({m["model"]})</span></td>' + cells + "</tr>"
        )

    css = (
        "body{margin:0;background:#0b0f19;color:#e5e7eb;font-family:Inter,system-ui,sans-serif;padding:28px 36px}"
        "h1{font-size:24px;margin:0 0 4px}.sub{color:#9ca3af;font-size:13px;margin-bottom:22px;max-width:920px;line-height:1.5}"
        "table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:8px}"
        "th,td{border:1px solid #1f2937;padding:7px 10px}thead th{background:#111827;color:#cbd5e1;font-weight:600}"
        ".axis{font-size:11px;color:#6b7280;letter-spacing:1px;text-transform:uppercase;margin:18px 0 6px}"
        "td.rl{text-align:left;white-space:nowrap}.lbl{font-weight:600}"
        ".key{display:block;font-family:monospace;font-size:9.5px;color:#5b6677;margin-top:1px}"
        "thead th{cursor:help}td[title]{cursor:help}"
        ".empty{background:#0f1629;border:1px solid #1f2937;border-radius:10px;padding:24px;color:#9ca3af;font-size:14px;line-height:1.6}"
        "code{background:#111827;padding:2px 6px;border-radius:4px;font-size:12px;color:#93c5fd}"
    )

    if not have_table or not labels:
        msg = (
            "<div class='empty'>No classification-matrix runs yet. Populate "
            "<code>kg.classify_runs</code> with the harness:<br><br>"
            "<code>cd ai-fan-out/backend &amp;&amp; .venv/bin/python -m journey.run_classify_matrix "
            "--run-label baseline_converge --strategy converge --prompt-mode baseline "
            "--augmentation facts+kg --model gpt-5-mini --personas naive_vague --limit 5</code>"
            "</div>"
        )
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>Classification matrix</title>"
            "<style>" + css + "</style></head><body><h1>Classification matrix</h1>"
            "<div class='sub'>Disambiguation analogue of the retrieval matrix: each cell is "
            "<b>gold-in-final-set %</b> (presence) for one config x persona.</div>" + msg
            + "</body></html>"
        )

    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Classification matrix</title>"
        "<style>" + css + "</style></head><body>"
        "<h1>Classification matrix</h1>"
        "<div class='sub'>The disambiguation analogue of the retrieval matrix. Rows = a Q&amp;A "
        "<b>config</b> (strategy &middot; prompt_mode &middot; augmentation); columns = personas "
        "(same item, 7 phrasings: novice/vague &rarr; ATAR-original). Each cell = <b>gold-in-final-set %</b> "
        "&mdash; how often the correct code is present ANYWHERE in the final committed/surviving set "
        "(PRESENCE, the primary metric - more important than rank). Hover a cell for median rank, "
        "survivor-set size, rounds and est $/session. <b>eliminate</b> rows (green badge) fix the "
        "candidate set at round 1 and only rule out; <b>converge</b> rows re-retrieve each round. "
        "Greener = better; blank = that config x persona not run yet.</div>"
        "<div class='axis'>&larr; same item, 7 phrasings: novice / vague &nbsp;...&nbsp; expert / ATAR-original &rarr;</div>"
        "<table><thead><tr><th style='text-align:left'>config (strategy &middot; prompt_mode &middot; augmentation)</th>"
        + header_cells + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"
        "</body></html>"
    )



# --- SPA serving (standalone container) ---
from pathlib import Path as _Path
from fastapi.responses import FileResponse as _FileResponse
from fastapi.staticfiles import StaticFiles as _StaticFiles
from fastapi import HTTPException as _HTTPException
_DIST = _Path(__file__).resolve().parents[2] / "frontend" / "dist"
if (_DIST / "assets").is_dir():
    app.mount("/assets", _StaticFiles(directory=str(_DIST / "assets"), check_dir=False), name="assets")
@app.get("/", include_in_schema=False)
def _spa_root() -> _FileResponse:
    return _FileResponse(str(_DIST / "index.html"), media_type="text/html")
@app.get("/{full_path:path}", include_in_schema=False)
def _spa_fallback(full_path: str):
    if full_path.startswith(("api/", "eval/")):
        raise _HTTPException(status_code=404, detail="Not found")
    return _FileResponse(str(_DIST / "index.html"), media_type="text/html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
