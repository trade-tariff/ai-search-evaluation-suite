"""Intercept-list complexity analysis.

Tabs the HMRC intercept term list, runs each through production-mirroring
hybrid retrieval (HybridRetrievalService.rb shape: RRF k=60, ef_search=100,
threshold 0.35, producline_suffix='80', hidden goods excluded), and computes
per-term KPIs at multiple section/chapter/heading levels plus an inflexion-
point question count.

Provides:
    list_terms()                     -> all 728 terms with metadata
    analyze(indices, k, weights)     -> per-term KPIs + retrieved candidates
    save_run(name, results)          -> persist a run to data/intercept_runs/
    list_runs()                      -> saved run metadata
    load_run(run_id)

The Retriever is a module-level singleton; setup precomputes the declarable
leaf set, chapter->section map, and indent-depth lookup once per process.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

import intercept_retrieval as _ir
import intercept_kpis as _kpis


DATA_DIR = Path(__file__).parent.parent / "data"
TERMS_PATH = DATA_DIR / "intercept_terms.json"
RUNS_DIR = DATA_DIR / "intercept_runs"
RUNS_DIR.mkdir(exist_ok=True)

# Production prompt template (the same one stored in search_contexts.json,
# which mirrors the live InteractiveSearchService context). Used by the
# generate-question endpoint so the differentiator question is what
# production AI search would actually ask.
_CONTEXT_TEMPLATE_PATH = DATA_DIR / "search_contexts.json"

QUESTION_MODEL = "gpt-5.5"
QUESTION_REASONING_EFFORT = "medium"


def _load_context_template() -> str | None:
    if not _CONTEXT_TEMPLATE_PATH.exists():
        return None
    try:
        return json.loads(_CONTEXT_TEMPLATE_PATH.read_text()).get("context_template")
    except Exception:
        return None


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_desc(s: str | None) -> str | None:
    """Strip HTML tags (`<br>`, `<br />`, etc) and collapse whitespace.
    The source data in `goods_nomenclature_descriptions` and self_texts
    contains raw `<br>` markers that should not surface in the UI."""
    if not s:
        return s
    cleaned = _HTML_TAG_RE.sub(" ", s)
    return re.sub(r"\s+", " ", cleaned).strip()


def _format_opensearch_results_for_prompt(candidates: list[dict[str, Any]]) -> str:
    """Mirror prompts._format_opensearch_results so the LLM sees the same
    candidate-list shape it sees in production."""
    lines: list[str] = []
    for i, r in enumerate(candidates, 1):
        code = r.get("goods_nomenclature_item_id") or r.get("commodity_code") or ""
        desc = (
            r.get("declarable_title")
            or r.get("search_text")
            or r.get("self_text")
            or r.get("description")
            or ""
        )
        desc = desc.replace("<br>", " ").replace("\n", " ")
        score = float(r.get("score") or r.get("cosine_score") or 0.0)
        lines.append(f"{i}. {code} - {desc} (score: {score:.3f})")
    return "\n".join(lines)


# ----- Singleton retriever -----------------------------------------------
_retriever: _ir.Retriever | None = None
_setup_lock = asyncio.Lock()


async def get_retriever(openai_api_key: str | None = None) -> _ir.Retriever:
    global _retriever
    async with _setup_lock:
        if _retriever is None:
            client = AsyncOpenAI(api_key=openai_api_key) if openai_api_key else AsyncOpenAI()
            r = _ir.Retriever(openai_client=client)
            await r.setup()
            _retriever = r
        elif openai_api_key:
            # API key may change at runtime via /api/config; swap the client.
            _retriever.openai = AsyncOpenAI(api_key=openai_api_key)
    return _retriever


# ----- Terms -------------------------------------------------------------
def list_terms() -> list[dict[str, Any]]:
    if not TERMS_PATH.exists():
        return []
    data = json.loads(TERMS_PATH.read_text())
    # Add a stable index so the frontend can address by position.
    return [{"index": i, **t} for i, t in enumerate(data)]


# ----- Analysis ----------------------------------------------------------
async def analyze_term(
    retriever: _ir.Retriever,
    term: str,
    k: int,
    over_fetch: int,
    weights: dict[str, float] | None,
    vector_threshold: float | None = None,
    max_options_per_question: int = _kpis.DEFAULT_MAX_OPTIONS_PER_QUESTION,
) -> dict[str, Any]:
    """Run retrieval + KPIs for one term. Returns row + raw candidates."""
    result = await retriever.retrieve(term, limit=over_fetch, vector_threshold=vector_threshold)
    cands = result["candidates"]
    # Inject section roman + per-level titles per candidate so the frontend
    # can label every tree box with a real description ("Chapter 90 = OPTICAL,
    # PHOTOGRAPHIC..."), not just numeric IDs.
    chap_to_sec = retriever._chapter_to_section
    section_titles = retriever._section_titles
    descs = retriever._descriptions_by_code
    ctx = retriever._contextualised_by_code  # AI-166 self_text at every level

    def _title(key: str | None) -> tuple[str | None, bool]:
        """Prefer AI-166 contextualised self_text over the raw description
        whenever the raw is the literal 'Other' (or absent). Returns
        (title, was_contextualised)."""
        if not key:
            return None, False
        raw = _clean_desc(descs.get(key))
        ctxd = _clean_desc(ctx.get(key))
        if ctxd and (raw is None or raw.strip().lower() == "other"):
            return ctxd, True
        return raw, False

    for c in cands:
        code = c.get("goods_nomenclature_item_id", "") or ""
        c["section"] = chap_to_sec.get(code[:2])
        c["section_title"] = _clean_desc(section_titles.get(c["section"])) if c["section"] else None
        # Build padded keys for each level cut.
        chap_key = code[:2].ljust(10, "0") if len(code) >= 2 else None
        head_key = code[:4].ljust(10, "0") if len(code) >= 4 else None
        sub_key = code[:6].ljust(10, "0") if len(code) >= 6 else None
        eight_key = code[:8].ljust(10, "0") if len(code) >= 8 else None
        c["chapter_title"], chap_ctx = _title(chap_key)
        c["heading_title"], head_ctx = _title(head_key)
        c["subheading_title"], sub_ctx = _title(sub_key)
        c["eight_digit_title"], eight_ctx = _title(eight_key)
        # Per-level contextualised flags so the UI can badge intermediate
        # boxes whose label came from AI-166 self_text, not the raw "Other".
        c["chapter_contextualised"] = chap_ctx
        c["heading_contextualised"] = head_ctx
        c["subheading_contextualised"] = sub_ctx
        c["eight_digit_contextualised"] = eight_ctx
        # Sanitize the raw fields too — they're surfaced as fallbacks and in
        # candidate tables.
        c["search_text"] = _clean_desc(c.get("search_text"))
        c["self_text"] = _clean_desc(c.get("self_text"))
        # AI-166: for "Other" leaves (generation_type='ai'), the raw description
        # is just "Other". The contextualised description lives in self_text —
        # prefer that so the tree shows what the AI sees, not "Other".
        raw_decl = _clean_desc(descs.get(code))
        if c.get("generation_type") == "ai" and c.get("self_text"):
            c["declarable_title"] = c["self_text"]
            c["contextualised_other"] = True
        else:
            c["declarable_title"] = raw_decl or c.get("search_text") or c.get("self_text")
            c["contextualised_other"] = False
    kpis = _kpis.compute(
        term=term,
        results=cands,
        k=k,
        chapter_to_section=retriever._chapter_to_section,
        indent_depth_by_sid=retriever._indent_depth_by_sid,
        weights=weights,
        max_options_per_question=max_options_per_question,
        vector_threshold=result.get("vector_threshold_used", 0.35) or 0.35,
    )
    return {
        "row": kpis.as_row(),
        "retrieval_meta": {
            "vector_count": result["vector_count"],
            "keyword_count": result["keyword_count"],
            "fused_count": result["fused_count"],
            "declarable_count": result["declarable_count"],
            "vector_threshold_used": result.get("vector_threshold_used"),
        },
        "top_candidates": cands[:k],
        "below_threshold_candidates": result.get("below_threshold_candidates", []),
    }


async def analyze_term_with_embedding(
    retriever: _ir.Retriever,
    term: str,
    embedding: list[float],
    k: int,
    over_fetch: int,
    weights: dict[str, float] | None,
    vector_threshold: float | None = None,
    max_options_per_question: int = _kpis.DEFAULT_MAX_OPTIONS_PER_QUESTION,
) -> dict[str, Any]:
    """Same shape as analyze_term but skips the embed call — caller provides
    a pre-computed embedding for `term`. Used by the batched-embed pipeline."""
    result = await retriever.retrieve_with_embedding(
        term, embedding, limit=over_fetch, vector_threshold=vector_threshold,
    )
    cands = result["candidates"]
    chap_to_sec = retriever._chapter_to_section
    section_titles = retriever._section_titles
    descs = retriever._descriptions_by_code
    ctx = retriever._contextualised_by_code

    def _title(key: str | None) -> tuple[str | None, bool]:
        if not key:
            return None, False
        raw = _clean_desc(descs.get(key))
        ctxd = _clean_desc(ctx.get(key))
        if ctxd and (raw is None or raw.strip().lower() == "other"):
            return ctxd, True
        return raw, False

    for c in cands:
        code = c.get("goods_nomenclature_item_id", "") or ""
        c["section"] = chap_to_sec.get(code[:2])
        c["section_title"] = _clean_desc(section_titles.get(c["section"])) if c["section"] else None
        chap_key = code[:2].ljust(10, "0") if len(code) >= 2 else None
        head_key = code[:4].ljust(10, "0") if len(code) >= 4 else None
        sub_key = code[:6].ljust(10, "0") if len(code) >= 6 else None
        eight_key = code[:8].ljust(10, "0") if len(code) >= 8 else None
        c["chapter_title"], chap_ctx = _title(chap_key)
        c["heading_title"], head_ctx = _title(head_key)
        c["subheading_title"], sub_ctx = _title(sub_key)
        c["eight_digit_title"], eight_ctx = _title(eight_key)
        c["chapter_contextualised"] = chap_ctx
        c["heading_contextualised"] = head_ctx
        c["subheading_contextualised"] = sub_ctx
        c["eight_digit_contextualised"] = eight_ctx
        c["search_text"] = _clean_desc(c.get("search_text"))
        c["self_text"] = _clean_desc(c.get("self_text"))
        raw_decl = _clean_desc(descs.get(code))
        if c.get("generation_type") == "ai" and c.get("self_text"):
            c["declarable_title"] = c["self_text"]
            c["contextualised_other"] = True
        else:
            c["declarable_title"] = raw_decl or c.get("search_text") or c.get("self_text")
            c["contextualised_other"] = False

    kpis = _kpis.compute(
        term=term,
        results=cands,
        k=k,
        chapter_to_section=retriever._chapter_to_section,
        indent_depth_by_sid=retriever._indent_depth_by_sid,
        weights=weights,
        max_options_per_question=max_options_per_question,
        vector_threshold=result.get("vector_threshold_used", 0.35) or 0.35,
    )
    return {
        "row": kpis.as_row(),
        "retrieval_meta": {
            "vector_count": result["vector_count"],
            "keyword_count": result["keyword_count"],
            "fused_count": result["fused_count"],
            "declarable_count": result["declarable_count"],
            "vector_threshold_used": result.get("vector_threshold_used"),
        },
        "top_candidates": cands[:k],
        "below_threshold_candidates": result.get("below_threshold_candidates", []),
    }


async def analyze(
    indices: list[int] | None,
    k: int,
    over_fetch: int,
    openai_api_key: str | None,
    weights: dict[str, float] | None,
    vector_threshold: float | None = None,
    max_options_per_question: int = _kpis.DEFAULT_MAX_OPTIONS_PER_QUESTION,
) -> dict[str, Any]:
    """Run analysis for the given term indices (or all when indices is None)."""
    terms = list_terms()
    if indices is not None:
        selected = [t for t in terms if t["index"] in set(indices)]
    else:
        selected = terms

    retriever = await get_retriever(openai_api_key)
    started_at = time.time()
    rows: list[dict[str, Any]] = []
    details: dict[str, dict[str, Any]] = {}

    for t in selected:
        term = t["term"]
        try:
            out = await analyze_term(
                retriever, term, k, over_fetch, weights, vector_threshold,
                max_options_per_question=max_options_per_question,
            )
        except Exception as exc:
            details[term] = {"error": repr(exc)}
            continue
        row = out["row"]
        # Carry spreadsheet metadata through to the row so the UI can filter:
        row["index"] = t["index"]
        row["count"] = t["count"]
        row["template"] = t["template"]
        row["source"] = t["source"]
        row["guidance_page"] = t["guidance_page"]
        row["decision"] = t["decision"]
        rows.append(row)
        details[term] = out

    elapsed = time.time() - started_at
    return {
        "k": k,
        "over_fetch": over_fetch,
        "weights": weights or _kpis.DEFAULT_WEIGHTS,
        "n_terms": len(selected),
        "elapsed_seconds": elapsed,
        "rows": rows,
        "details": details,
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# ----- LLM differentiator question generation ----------------------------
async def generate_inflection_question(
    *,
    term: str,
    candidates: list[dict[str, Any]],
    openai_api_key: str | None,
    model: str = QUESTION_MODEL,
    reasoning_effort: str = QUESTION_REASONING_EFFORT,
    breadcrumb: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Ask the LLM to produce the multi-option question it would ask at a tree
    node, using the exact production InteractiveSearchService prompt template.

    `candidates` should be the candidate set under the inflection node — i.e.
    the descendants whose branch the LLM is choosing between. The LLM sees
    these as OpenSearch results.

    `breadcrumb`, if provided, is the path from root to the current node; it
    gets stuffed into the QUESTIONS_AND_ANSWERS slot as the prior context so
    the LLM knows what has already been resolved.
    """
    if not openai_api_key:
        raise RuntimeError("openai_api_key required to generate a question")

    template = _load_context_template()
    if not template:
        raise RuntimeError(
            f"Production context template not found at {_CONTEXT_TEMPLATE_PATH}"
        )

    qa_history_text = "No previous questions."
    if breadcrumb:
        qa_history_text = "\n".join(
            f"Q{i+1}: At {step.get('level')}, narrowed to {step.get('label')} ({step.get('description') or 'no description'})\nA{i+1}: chosen"
            for i, step in enumerate(breadcrumb)
        )

    filled = (
        template.replace("%{search_input}", term)
        .replace("%{expanded_query}", term)
        .replace("%{answers_opensearch}", _format_opensearch_results_for_prompt(candidates))
        .replace("%{questions}", qa_history_text)
    )

    client = AsyncOpenAI(api_key=openai_api_key)
    started = time.time()
    try:
        resp = await client.chat.completions.create(
            model=model,
            reasoning_effort=reasoning_effort,
            messages=[
                {"role": "system", "content": filled},
                {"role": "user", "content": f"Classify: {term}"},
            ],
        )
    except Exception as exc:
        return {"error": f"LLM call failed: {exc!r}"}

    raw = resp.choices[0].message.content or ""
    elapsed = time.time() - started

    # Strip ``` fences if present, then locate the trailing JSON block.
    cleaned = raw.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        # Take the first ```...``` block that looks like JSON
        for part in parts:
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{") and p.endswith("}"):
                cleaned = p
                break
    parsed: dict[str, Any] = {}
    try:
        parsed = json.loads(cleaned)
    except Exception:
        # Last-resort: scan for the first { and the matching closing }
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first >= 0 and last > first:
            try:
                parsed = json.loads(cleaned[first : last + 1])
            except Exception:
                parsed = {}

    out: dict[str, Any] = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "elapsed_seconds": round(elapsed, 2),
        "raw": raw,
    }
    if isinstance(parsed.get("questions"), list) and parsed["questions"]:
        q = parsed["questions"][0]
        out["question"] = q.get("question")
        out["options"] = q.get("options") or []
    elif isinstance(parsed.get("answers"), list):
        out["answers"] = parsed["answers"]
    elif parsed.get("error"):
        out["error"] = parsed["error"]
    else:
        out["error"] = "Could not parse questions/answers from LLM response"
    return out


# ----- Saved runs --------------------------------------------------------
def save_run(name: str, payload: dict[str, Any]) -> str:
    run_id = uuid.uuid4().hex[:8]
    rec = {"id": run_id, "name": name, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), **payload}
    path = RUNS_DIR / f"run_{run_id}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False))
    return run_id


_LIST_RUNS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
# Top-level metadata fields are the only ones we need for the dropdown. The
# 788MB commodity-sweep run.json files have `rows` and `details` keys taking
# most of the bytes — we never need those for the listing. By caching per
# (path, mtime) we parse each file once per its lifetime instead of on every
# /api/intercepts/runs call.

def _read_run_metadata(p) -> dict[str, Any] | None:
    """Cached metadata read. Re-parses only when the file's mtime changes."""
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    key = str(p)
    cached = _LIST_RUNS_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    meta = {
        "id": d.get("id"),
        "name": d.get("name"),
        "saved_at": d.get("saved_at"),
        "n_terms": d.get("n_terms"),
        "k": d.get("k"),
        "kind": d.get("kind"),
        "status": d.get("status"),
        "recall_metrics": d.get("recall_metrics"),
        "bucket_counts": d.get("bucket_counts"),
    }
    _LIST_RUNS_CACHE[key] = (mtime, meta)
    return meta


def list_runs() -> list[dict[str, Any]]:
    out = []
    for p in sorted(RUNS_DIR.glob("run_*.json")):
        # Skip the .scatter.json companions — we only want the main run files.
        if ".scatter" in p.name:
            continue
        meta = _read_run_metadata(p)
        if meta is not None:
            out.append(meta)
    return sorted(out, key=lambda r: r.get("saved_at") or "", reverse=True)


def load_run(run_id: str) -> dict[str, Any] | None:
    path = RUNS_DIR / f"run_{run_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


# ----- Action-bucket classifier (Day 0.5) --------------------------------
#
# Maps a retrieval row + its candidate set to one of two operator action
# buckets (A=AI can classify, B=needs intervention) and, for bucket B, a
# recommended action lane. Deterministic — no LLM in the loop. Validated
# against the 728-term HMRC ground truth at 90.4% recall on `Hard-to-classify`
# + `Escalate`. Will be refined later with a mined fork-predicate vocabulary.

_EXPERT_MARKERS = re.compile(
    r"\b("
    r"cas\b|"
    r"polymerisation|polymer\s+(degree|chain)|"
    r"denier|staple\s+length|"
    r"by\s+weight|%\s*by\s+weight|"
    r"density\s*(of|>=|>|<|of\s+\d)|"
    r"viscosity|kinematic|"
    r"civil\s+aircraft|for\s+aircraft|"
    r"end[-\s]use|"
    r"degrees?\s+(brix|baum[ée])|"
    r"empirical\s+formula|"
    r"isomer|stereoisomer|enantiomer|"
    r"chemical(ly)?\s+(modified|derived|defined)|"
    r"medicament|prophylactic|therapeutic|"
    r"chapter\s+note|note\s+\d|"
    r"binding\s+tariff"
    r")\b",
    re.IGNORECASE,
)


def _has_expert_marker(text: str | None) -> bool:
    return bool(text and _EXPERT_MARKERS.search(text))


# Fork-predicate inventory — loaded lazily once predicate_miner.py has run.
# When available, lookup by (parent_code, child_code) returns the predicate's
# askability bucket ("observable" / "document" / "lab_expert" / "legal" /
# "residual"). This is the principled replacement for the regex on raw
# candidate descriptions — much more accurate when forks share boilerplate
# text but differ on a specific clause.
_PREDICATE_INVENTORY: dict[tuple[str, str], str] | None = None
_PREDICATE_INVENTORY_MTIME: float = 0.0


def _load_predicate_inventory() -> dict[tuple[str, str], str] | None:
    """Lazy-load the predicate inventory. Re-loads when the file's mtime
    changes — so a fresh miner run is picked up automatically without
    restarting the running backend."""
    global _PREDICATE_INVENTORY, _PREDICATE_INVENTORY_MTIME
    path = DATA_DIR / "tariff_fork_predicates.json"
    if not path.exists():
        return _PREDICATE_INVENTORY  # may be None or a stale-good copy
    try:
        mtime = path.stat().st_mtime
        if mtime == _PREDICATE_INVENTORY_MTIME and _PREDICATE_INVENTORY is not None:
            return _PREDICATE_INVENTORY
        data = json.loads(path.read_text())
        records = data.get("predicates", [])
        # Skip seed-only runs (>80% residual = no real LLM classification yet)
        non_residual = sum(1 for r in records if r.get("bucket") != "residual")
        if records and non_residual < len(records) * 0.2:
            return _PREDICATE_INVENTORY  # don't replace good inventory with seed-only
        _PREDICATE_INVENTORY = {
            (r["parent_code"], r["child_code"]): r["bucket"]
            for r in records
        }
        _PREDICATE_INVENTORY_MTIME = mtime
        return _PREDICATE_INVENTORY
    except Exception:
        return _PREDICATE_INVENTORY


def _lookup_walked_path_predicates(candidate_code: str) -> list[str]:
    """For a candidate's code, return the predicate-bucket types at each
    level of its lineage (chapter→heading→subheading→8-digit→declarable).
    Empty list when the inventory isn't loaded or no forks match."""
    inv = _load_predicate_inventory()
    if not inv or not candidate_code or len(candidate_code) < 10:
        return []
    out: list[str] = []
    # Walk parent→child pairs at each digit-boundary
    boundaries = [(2, 4), (4, 6), (6, 8), (8, 10)]
    for parent_n, child_n in boundaries:
        parent = candidate_code[:parent_n].ljust(10, "0")
        child = candidate_code[:child_n].ljust(10, "0")
        bucket = inv.get((parent, child))
        if bucket:
            out.append(bucket)
    return out


def classify_action_bucket(row: dict[str, Any], top_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Heuristic action-bucket classifier (proxy until predicate inventory exists).

    Returns: {"bucket": "A"|"B", "lane": str|None, "intercept_type": str|None, "reason": str}

    Lanes:
      - "intercept"           — apply a description.* or commodity.* intercept
      - "annotate_ai166_fix"  — flag the AI-166 self_text for rewrite
      - None                  — bucket A, no action needed
    """
    n = row.get("n_results", 0)
    n_section = row.get("n_section", 0)
    n_chapter = row.get("n_chapter", 0)
    top_chapter_share = row.get("top_chapter_share", 0.0)
    score_flatness = row.get("score_flatness", 0.0)
    other_leaf_share = row.get("other_leaf_share", 0.0)

    marker_hits = 0
    for c in top_candidates[:10]:
        text = c.get("self_text") or c.get("declarable_title") or c.get("search_text") or ""
        if _has_expert_marker(text):
            marker_hits += 1
    expert_dominant = marker_hits >= 3

    # Calibrated against both 728 (vague user terms) and the 30-code validation
    # set (specific product queries). Key signals, in priority order:
    #   1. retrieval miss / very low n_results
    #   2. expert/legal differentiators in TOP-cosine candidates (not fringe)
    #   3. genuinely cross-domain spread (n_section >= 4 is hard to fake)
    #   4. clearly vague: high flatness AND many chapters
    #   5. n.e.s. dominant — flag for AI-166 rewrite
    # Per-code-query distributions look very different from per-term-search
    # distributions; we accept a 70-80% recall band on both rather than tune
    # to either extreme.

    if n == 0:
        return {"bucket": "B", "lane": "intercept", "intercept_type": "description.guidance",
                "reason": "retrieval miss — no candidates"}

    # Predicate-inventory check: for each top-cosine candidate, look up the
    # walked-path fork-predicate types. If many lab_expert/legal predicates
    # appear on the path, the trader can't answer the differentiator — route
    # to guidance, not AI Q&A. This supersedes the regex-on-text approach
    # whenever the inventory is loaded.
    top_cos = max((c.get("cosine_score") or 0) for c in top_candidates[:10]) if top_candidates else 0
    expert_predicate_hits = 0
    legal_predicate_hits = 0
    for c in top_candidates[:10]:
        cos = c.get("cosine_score") or 0
        if cos < top_cos * 0.85:
            continue
        bucket_seq = _lookup_walked_path_predicates(c.get("goods_nomenclature_item_id", ""))
        if "lab_expert" in bucket_seq:
            expert_predicate_hits += 1
        if "legal" in bucket_seq:
            legal_predicate_hits += 1
    if expert_predicate_hits >= 3:
        return {"bucket": "B", "lane": "intercept", "intercept_type": "description.guidance",
                "reason": f"lab/expert predicates on walked path for {expert_predicate_hits}/10 top candidates"}
    if legal_predicate_hits >= 3:
        return {"bucket": "B", "lane": "intercept", "intercept_type": "description.guidance",
                "reason": f"legal/contextual predicates on walked path for {legal_predicate_hits}/10 top candidates"}

    # Regex fallback (used when inventory isn't loaded, or as backup signal)
    marker_hits_strong = 0
    for c in top_candidates[:10]:
        cos = c.get("cosine_score") or 0
        if cos < top_cos * 0.85:
            continue
        text = c.get("self_text") or c.get("declarable_title") or c.get("search_text") or ""
        if _has_expert_marker(text):
            marker_hits_strong += 1
    if marker_hits_strong >= 4:
        return {"bucket": "B", "lane": "intercept", "intercept_type": "description.guidance",
                "reason": f"expert markers in {marker_hits_strong}/10 top-cosine candidates"}

    # Genuinely cross-domain spread — 4+ sections rarely happens unless
    # the query is truly vague (e.g. "gift", "accessory", "part")
    if n_section >= 4:
        return {"bucket": "B", "lane": "intercept", "intercept_type": "description.exclude",
                "reason": f"cross-domain spread ({n_section} sections)"}

    # 3-section spread WITH flat scores
    if n_section >= 3 and score_flatness >= 0.5:
        return {"bucket": "B", "lane": "intercept", "intercept_type": "description.exclude",
                "reason": f"cross-section spread ({n_section}) + flat scores ({score_flatness:.2f})"}

    # Many chapters + flat = vague within-domain
    if n_chapter >= 5 and score_flatness >= 0.7:
        return {"bucket": "B", "lane": "intercept", "intercept_type": "description.filter",
                "reason": f"{n_chapter} chapters + flat scores ({score_flatness:.2f})"}

    # 2-section split with flat scores
    if n_section == 2 and score_flatness >= 0.7:
        return {"bucket": "B", "lane": "intercept", "intercept_type": "description.filter",
                "reason": f"two-section spread + flat scores ({score_flatness:.2f})"}

    if other_leaf_share >= 0.5:
        return {"bucket": "B", "lane": "annotate_ai166_fix", "intercept_type": None,
                "reason": f"{other_leaf_share:.0%} of top-K are n.e.s. catch-alls"}

    return {"bucket": "A", "lane": None, "intercept_type": None,
            "reason": "retrieval converges on coherent narrow set"}


# ----- All-commodities sweep ---------------------------------------------
async def list_declarable_commodities(
    retriever: _ir.Retriever,
    sample_size: int | None = None,
) -> list[dict[str, Any]]:
    """Return every declarable UK commodity with its query text.

    Each item: {sid, code, query, chapter, section}.
    `query` is the cleaned first sentence of self_text (or search_text /
    description as fallback) — what we feed the retrieval pipeline as the
    "user input" for that commodity.

    `sample_size`, when set, picks N evenly-spaced commodities (deterministic
    stratification across the sorted code list) so you can validate the
    pipeline without burning a full 14k-call run.
    """
    assert retriever._pool
    async with retriever._pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                gn.goods_nomenclature_sid AS sid,
                gn.goods_nomenclature_item_id AS code,
                st.self_text,
                st.search_text
            FROM uk.goods_nomenclatures gn
            -- INNER JOIN: only commodities AI-166 actually contextualised.
            -- These already carry good queryable text — no need to climb the
            -- hierarchy or fall back to raw 'Other' descriptions.
            JOIN uk.goods_nomenclature_self_texts st USING (goods_nomenclature_sid)
            WHERE gn.producline_suffix = $1
              -- Currently in force only (exclude expired historical versions).
              AND gn.validity_start_date <= CURRENT_DATE
              AND (gn.validity_end_date IS NULL OR gn.validity_end_date > CURRENT_DATE)
              AND st.self_text IS NOT NULL
              AND LENGTH(TRIM(st.self_text)) >= 20
              AND gn.goods_nomenclature_item_id NOT IN (
                  SELECT goods_nomenclature_item_id FROM uk.hidden_goods_nomenclatures
              )
            ORDER BY gn.goods_nomenclature_item_id
            """,
            _ir.NON_GROUPING_PRODUCTLINE_SUFFIX,
        )

    declarable_sids = retriever._declarable_sids or set()

    items: list[dict[str, Any]] = []
    for r in rows:
        if r["sid"] not in declarable_sids:
            continue
        # AI-166 already wrote a contextualised self_text for every commodity
        # in this filtered set. Trim to first sentence + cap length so the
        # embedding stays focused on the actual description rather than the
        # "Also known as" synonyms appended for retrieval recall.
        cleaned = _clean_desc(r["self_text"]) or ""
        first = cleaned.split("\n", 1)[0]
        query = first[:200].strip()
        if not query:
            continue
        code = r["code"]
        items.append({
            "sid": r["sid"],
            "code": code,
            "query": query,
            "chapter": code[:2] if len(code) >= 2 else None,
            "section": retriever._chapter_to_section.get(code[:2]),
        })

    if sample_size and len(items) > sample_size:
        step = len(items) / sample_size
        items = [items[int(i * step)] for i in range(sample_size)]
    return items


# ----- LLM paraphrase: turn a tariff self_text into an ordinary user query
PARAPHRASE_MODEL = "gpt-5-mini"  # cheap; tighter prompt control

_PARAPHRASE_SYSTEM = """You are simulating a non-expert UK trader using HMRC's tariff search.
Given a commodity description, write what an ordinary trader would type to find it.

Rules:
- 2 to 8 words
- Plain English, no tariff jargon
- NO commodity codes, HS chapter numbers, or heading numbers
- NO words: "other", "n.e.s.", "not elsewhere specified", "excluding", "excl."
- NO legal/expert qualifiers like CAS, polymerisation, denier, by weight
- NO copying long phrases from the description verbatim
- Just describe the product as a trader holding it would describe it

Return ONLY the search phrase, nothing else. No quotes, no explanation."""


_FORBIDDEN_QUERY_TOKENS = re.compile(
    r"\b("
    r"n\.?\s*e\.?\s*s\.?|"
    r"not\s+elsewhere\s+specified|"
    r"excl\.?|excluding|"
    r"cas\s*\d|"
    r"polymerisation|denier|by\s+weight|"
    r"chapter\s+\d|heading\s+\d|"
    r"\d{4,}"  # 4+ digit numbers are usually codes
    r")\b",
    re.IGNORECASE,
)


def _is_acceptable_query(query: str, source: str) -> bool:
    """Post-filter: reject queries that violate the strict prompt rules."""
    q = (query or "").strip()
    if not q:
        return False
    words = q.split()
    if len(words) < 2 or len(words) > 12:
        return False
    if _FORBIDDEN_QUERY_TOKENS.search(q):
        return False
    # Reject if too much overlap with source self_text (heuristic — first 60 chars)
    source_prefix = (source or "").lower()[:60]
    if source_prefix and source_prefix in q.lower():
        return False
    return True


_TIERED_SYSTEM = """You are simulating a non-expert UK trader using HMRC's tariff search.
Given a commodity description, write THREE search phrases at different specificity levels.

GENERIC tier: 1-3 words, vague noun phrase a trader might try first
   e.g. "phone case", "steel pipe", "frozen food"
ORDINARY tier: 2-6 words, plain product with one or two attributes
   e.g. "mobile phone case", "stainless steel pipe", "frozen pizza"
SPECIFIC tier: 4-10 words, complete trade-style description without tariff jargon
   e.g. "moulded plastic protective case for mobile phone", "seamless stainless steel pipe 10mm"

Rules for ALL tiers:
- Plain English, no tariff jargon
- NO commodity codes, HS chapter numbers, heading numbers
- NO words: "other", "n.e.s.", "not elsewhere specified", "excluding", "excl."
- NO legal/expert qualifiers like CAS, polymerisation, denier, by weight
- NO copying long phrases from the description verbatim

Reply in JSON only, no other text:
{"generic": "...", "ordinary": "...", "specific": "..."}"""


async def generate_user_queries_tiered(
    client: AsyncOpenAI,
    source_text: str,
    model: str = PARAPHRASE_MODEL,
    max_retries: int = 2,
) -> dict[str, str] | None:
    """Generate generic/ordinary/specific tier queries in one LLM call.

    Returns a dict {"generic": ..., "ordinary": ..., "specific": ...} or None
    on persistent failure.
    """
    src = (source_text or "").strip()
    if not src:
        return None
    for _ in range(max_retries + 1):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _TIERED_SYSTEM},
                    {"role": "user", "content": src[:400]},
                ],
                response_format={"type": "json_object"},
            )
            content = (resp.choices[0].message.content or "").strip()
            data = json.loads(content)
            tiers = {}
            for tier in ("generic", "ordinary", "specific"):
                q = (data.get(tier) or "").strip().strip("\"' .,;:\n\t")
                if _is_acceptable_query(q, src):
                    tiers[tier] = q
            if len(tiers) == 3:
                return tiers
        except Exception:
            pass
    return None


_BATCH_PARAPHRASE_SYSTEM = """For each tariff commodity description below, write what an
ordinary trader (not a tariff specialist) would type into a search box to find it.

Rules for EACH paraphrase:
- 2 to 8 words
- Plain English, no tariff jargon
- NO commodity codes, HS chapter numbers, or heading numbers
- NO words: "other", "n.e.s.", "not elsewhere specified", "excluding", "excl."
- NO legal/expert qualifiers like CAS, polymerisation, denier, by weight
- NO copying long phrases from the description verbatim

Reply with ONLY a JSON object: {"queries": ["query 1", "query 2", ...]}
The queries list MUST be the same length and order as the input list."""


async def generate_user_queries_batch(
    client: AsyncOpenAI,
    source_texts: list[str],
    model: str = PARAPHRASE_MODEL,
) -> list[str | None]:
    """Paraphrase N commodities in one LLM call. ~10x faster wall-clock than
    individual calls when N >= 10. Returns a list aligned to source_texts;
    entries that fail the post-filter are None — caller falls back to source_text."""
    if not source_texts:
        return []
    valid_sources = [(i, (t or "").strip()) for i, t in enumerate(source_texts)]
    valid_sources = [(i, t) for i, t in valid_sources if t]
    if not valid_sources:
        return [None] * len(source_texts)
    user_lines = "\n".join(f"{i+1}. {t[:300]}" for i, (_, t) in enumerate(valid_sources))
    out: list[str | None] = [None] * len(source_texts)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _BATCH_PARAPHRASE_SYSTEM},
                {"role": "user", "content": user_lines},
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads((resp.choices[0].message.content or "{}").strip())
        queries = data.get("queries", [])
        if not isinstance(queries, list):
            return out
        for (orig_idx, src), q in zip(valid_sources, queries):
            if not isinstance(q, str):
                continue
            q = q.strip().strip("\"' .,;:\n\t")
            if _is_acceptable_query(q, src):
                out[orig_idx] = q
    except Exception:
        pass
    return out


async def generate_user_query(
    client: AsyncOpenAI,
    source_text: str,
    model: str = PARAPHRASE_MODEL,
    max_retries: int = 2,
) -> str | None:
    """Generate a single ordinary-tier user query from a commodity self_text.

    Returns the query string, or None if all retries failed the post-filter.
    Caller falls back to the source_text if None is returned (preserves coverage).
    """
    src = (source_text or "").strip()
    if not src:
        return None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _PARAPHRASE_SYSTEM},
                    {"role": "user", "content": src[:400]},
                ],
            )
            candidate = (resp.choices[0].message.content or "").strip()
            # Strip surrounding quotes/punctuation
            candidate = candidate.strip("\"' .,;:\n\t")
            if _is_acceptable_query(candidate, src):
                return candidate
        except Exception:
            pass
    return None


async def analyze_all_commodities_iter(
    retriever: _ir.Retriever,
    k: int,
    over_fetch: int,
    weights: dict[str, float] | None,
    vector_threshold: float | None = None,
    sample_size: int | None = None,
    max_options_per_question: int = _kpis.DEFAULT_MAX_OPTIONS_PER_QUESTION,
    query_strategy: str = "self_text",
    openai_api_key: str | None = None,
):
    """Async generator yielding (commodity, kpi_row, error) per item.

    `query_strategy`:
      - "self_text"  : feed the commodity's own self_text into retrieval (the
                       original NEIGHBOUR DENSITY measurement — measures sibling
                       crowding, not user-facing difficulty)
      - "paraphrase" : LLM-generate an ordinary-tier user query from the self_text
                       and feed THAT into retrieval (CLASSIFICATION DIFFICULTY —
                       measures how a real-ish user query lands)

    For "paraphrase" mode, each row's `query` and `query_strategy` fields are
    set so the frontend can show what the LLM produced. Also runs the action-
    bucket classifier and sets row["bucket"], row["lane"], row["intercept_type"].

    Caller wraps in SSE. Errors are yielded as (item, None, error_str).
    """
    items = await list_declarable_commodities(retriever, sample_size=sample_size)

    paraphrase_client: AsyncOpenAI | None = None
    if query_strategy == "paraphrase":
        if not openai_api_key:
            raise RuntimeError("paraphrase strategy requires openai_api_key")
        paraphrase_client = AsyncOpenAI(api_key=openai_api_key)

    for it in items:
        query_used = it["query"]
        if paraphrase_client is not None:
            generated = await generate_user_query(paraphrase_client, it["query"])
            if generated:
                query_used = generated
            # else: keep the self_text fallback so we don't silently lose coverage
        try:
            out = await analyze_term(
                retriever, query_used, k, over_fetch, weights, vector_threshold,
                max_options_per_question=max_options_per_question,
            )
        except Exception as exc:
            yield it, None, repr(exc)
            continue
        row = out["row"]
        row["code"] = it["code"]
        row["sid"] = it["sid"]
        row["chapter"] = it["chapter"]
        row["section"] = it["section"]
        row["query"] = query_used
        row["source_self_text"] = it["query"]   # the original self_text we paraphrased from
        row["query_strategy"] = query_strategy
        # Apply the action-bucket classifier. For "paraphrase" runs this is the
        # real classification-difficulty signal; for "self_text" runs it's still
        # informative (catches sibling-crowded codes) but should be interpreted
        # as "is this code's neighbourhood crowded?" not "is it hard to find?".
        classification = classify_action_bucket(row, out.get("top_candidates", []))
        row["bucket"] = classification["bucket"]
        row["lane"] = classification["lane"]
        row["intercept_type"] = classification["intercept_type"]
        row["bucket_reason"] = classification["reason"]
        # Attach top_candidates to the yielded item for the SSE caller to
        # decide whether to ship them (Sample 200: yes, full 14k: optional).
        it["top_candidates"] = out.get("top_candidates", [])
        yield it, row, None
