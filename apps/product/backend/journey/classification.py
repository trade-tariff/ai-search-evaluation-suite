"""AI Guided Search classification, modelled directly on the OTT Interactive
Search Q&A loop (the same prompt template the ai-fan-out app benchmarks).

Per turn:
  1. Retrieve top-N candidates from the local tariff DB (hybrid: curated
     search_references + FTS + pgvector + RRF).
  2. Build the prompt with the real OTT context template, including any
     prior Q&A history.
  3. Call the LLM (gpt-5.5) for a JSON response:
       - {"answers": [{commodity_code, confidence}, ...]} when confident
       - {"questions": [{question, options}, ...]} when more info needed
       - {"error": "..."} when contradictory or unclassifiable
  4. Surface the chosen response to the FE.

KG + Facets are kept as an OPTIONAL ENRICHMENT - if the final code has a
hand-authored fact sheet in the slice we expose it. Nothing in this critical
path is gated by the slice; works for any of the 25k UK commodities.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from . import local_db as _local_db
from .local_db import (
    commodity as db_commodity,
    facets_for_codes as db_facets_for_codes,
    facet_definitions as db_facet_defs,
    kg_edges_for_candidates as db_kg_edges,
    retrieve_candidates as db_retrieve,
    display_descriptions_for_codes as db_display_descriptions_for_codes,
)
from .provider_guard import openai_allowed, provider_calls_allowed

# Per-request timeout for every OpenAI call. gpt-5*/o* are reasoning models and
# will silently hang a worker without one. Overridable via env for slow models.
LLM_TIMEOUT_S = float(os.environ.get("CLASSIFY_LLM_TIMEOUT_S", "90"))

# Prompt variants selectable via config['prompt_mode']. "baseline" is the
# current behaviour; the rest layer extra reasoning scaffolding into the
# system/user prompt. They are additive - an unknown value falls back to
# baseline so the existing flow never breaks.
PROMPT_MODES = {
    "baseline",
    "rule_reasoning",
    "exclusion_aware",
    "gir_citation",
    "self_verify",
    "rank_all",
    "top_k_pressure",
    "facet_soft_score",
}


def _apply_llm_tuning(kwargs: dict, model: str) -> dict:
    """Mutate+return an OpenAI chat.completions kwargs dict with the model-aware
    tuning every call site needs:
      - per-request timeout (always)
      - reasoning_effort='low' for gpt-5*/o* reasoning models (latency/cost)
      - temperature=0.0 for non-reasoning models (determinism)
    Reasoning models reject `temperature`, so it is only set off the gpt-5/o path.
    """
    kwargs.setdefault("timeout", LLM_TIMEOUT_S)
    if model.startswith("gpt-5.5"):
        # gpt-5.5 rejects reasoning_effort alongside function tools on
        # /v1/chat/completions (that combo requires /v1/responses). Use the
        # model default reasoning; send neither effort nor temperature.
        pass
    elif model.startswith("gpt-5") or model.startswith("o"):
        kwargs.setdefault("reasoning_effort", os.environ.get("CLASSIFY_REASONING_EFFORT", "low"))
    else:
        kwargs.setdefault("temperature", 0.0)
    return kwargs


# --- Prompt-mode scaffolding (config['prompt_mode']) --------------------

_RULE_REASONING_BLOCK = (
    "\n\n--- CLASSIFICATION REASONING (rule_reasoning mode) ---\n"
    "Before deciding, decompose BOTH the product and each plausible candidate along these axes:\n"
    "  - material (what it is made of)\n"
    "  - form (its physical form / state / presentation)\n"
    "  - function (what it does / its intended use)\n"
    "  - essential_character (the single factor that gives the whole its essential character, per GIR 3(b))\n"
    "Score each candidate per-axis against the product, then choose using GIR priority order:\n"
    "  GIR 1 (heading text + section/chapter notes) > GIR 2 > GIR 3(a) most-specific >\n"
    "  GIR 3(b) essential character > GIR 3(c) last-in-numerical-order > GIR 4 > GIR 5/6.\n"
    "Prefer the candidate whose material/form/function/essential-character best matches by the HIGHEST-priority "
    "GIR that resolves the choice. Do not let a lower GIR override a higher one."
)

_GIR_CITATION_BLOCK = (
    "\n\n--- JUSTIFICATION (gir_citation mode) ---\n"
    "For your top pick you MUST cite the specific GIR rule and/or the section/chapter note clause that "
    "justifies it (e.g. 'GIR 1 + Chapter 84 Note 2(a)', 'GIR 3(b) essential character'). "
    "If you cannot point to a clause that supports a code, do not rank it first."
)

_RANK_ALL_BLOCK = (
    "\n\n--- RANKING POLICY (rank_all mode) ---\n"
    "Do not stop at the first plausible code. Compare all candidate codes in the supplied shortlist. "
    "Rank the strongest five by fit to the trader's description, prior answers, tariff descriptions, "
    "structured facts, labels, and binding KG rules. Preserve weak alternatives only when the evidence "
    "does not let you rank them below the top five."
)

_TOP_K_PRESSURE_BLOCK = (
    "\n\n--- TOP-K PRESSURE (top_k_pressure mode) ---\n"
    "The retrieval shortlist may contain broad near-matches. Your job is to improve rank, not simply "
    "echo retrieval order. Prefer a concise ranked answer when the top candidates differ in material, "
    "form, function, thresholds, or legal exclusions. Ask a question only when one trader-answerable "
    "fact would materially change the top-ranked code."
)

_FACET_SOFT_SCORE_BLOCK = (
    "\n\n--- SOFT FACET SCORING (facet_soft_score mode) ---\n"
    "Use structured facts and KG rules as scoring evidence, not brittle elimination gates. "
    "Boost candidates whose facts, labels, notes, thresholds, ATAR rationale, HSEN guidance, "
    "footnotes, or measure conditions match the product. Demote candidates only for explicit "
    "contradictions, binding exclusions, or very high-confidence incompatibility. When facts are "
    "missing for a candidate, treat that as uncertainty rather than proof against it."
)


def _exclusion_notes_block(codes: list[str], include: Optional[dict] = None) -> str:
    """Pull triggered chapter/section exclusion edges (kg.kg_edges type='exclusion')
    in scope for the candidate set. Used by prompt_mode='exclusion_aware'.
    Returns '' if none apply."""
    if not codes:
        return ""
    try:
        edges = db_kg_edges(codes, include=include)
    except Exception:
        return ""
    excl = [e for e in edges if (e.get("type") == "exclusion")]
    if not excl:
        return ""

    def _scope_rank(e):
        s = e.get("scope") or ""
        if s.startswith("heading:"):
            return 0
        if s.startswith("chapter:"):
            return 1
        if s.startswith("section:"):
            return 2
        return 3

    excl.sort(key=lambda e: (_scope_rank(e), int(e.get("authority_tier") or 8)))
    lines = [
        "\n\n--- EXCLUSION NOTES (exclusion_aware mode) ---",
        "The following section/chapter EXCLUSION notes are in scope for this candidate set. "
        "Treat them as HARD rules: if a note excludes a kind of good from a chapter/heading, "
        "DEMOTE or drop any candidate that falls under that excluded branch.",
    ]
    for e in excl[:12]:
        body = (e.get("body") or "")[:400]
        lines.append(f"  [{e.get('scope','?')}] {e.get('title','?')}: {body}")
    if len(excl) > 12:
        lines.append(f"  ... plus {len(excl) - 12} more exclusion notes not shown.")
    return "\n".join(lines)


def _prompt_mode_system_suffix(prompt_mode: str) -> str:
    """Extra system-prompt text for a prompt_mode (excludes exclusion_aware,
    which needs the candidate codes and is built separately)."""
    if prompt_mode == "rule_reasoning":
        return _RULE_REASONING_BLOCK
    if prompt_mode == "gir_citation":
        return _GIR_CITATION_BLOCK
    if prompt_mode == "rank_all":
        return _RANK_ALL_BLOCK
    if prompt_mode == "top_k_pressure":
        return _TOP_K_PRESSURE_BLOCK
    if prompt_mode == "facet_soft_score":
        return _FACET_SOFT_SCORE_BLOCK
    return ""


# Default classification config. Provider-backed calls are opt-in; the offline
# deterministic path is the safe default for local demos and EC2 boot.
DEFAULT_CLASSIFY_CONFIG: dict = {
    # Disambiguation strategy: 'converge' (re-retrieve+recommit each round, the
    # original flow) or 'eliminate' (fix candidate set at round 1, only rule out
    # definitively-excluded candidates, present survivors ranked). See qa_loop.
    "strategy": "converge",
    "use_llm_candidate_selection": False,
    "candidate_selection_model": os.environ.get("CLASSIFY_LLM_MODEL", "gpt-5-nano"),
    "qa_mode": "ask_first",         # ask_first | answers_only
    # Prompt variant (see PROMPT_MODES): baseline | rule_reasoning |
    # exclusion_aware | gir_citation | self_verify.
    "prompt_mode": "baseline",
    "use_query_expansion": False,   # AI-441 - pre-LLM rewrite of query (default off; opt-in)
    "query_expansion_model": os.environ.get("TRIAGE_MODEL", "gpt-5-mini"),
    "query_expansion_prompt_variant": "mine",
    "use_facets": True,             # STRUCTURED_FACTS section
    "use_kg_prompt_context": True,  # Include shortlist facts + KG rules in the provider prompt.
    "use_session_facts": True,      # Extract facets from user input, re-rank + inject as HARD constraints
    # AI-440 style: server picks facet via entropy + builds options; LLM either
    # phrases the question or commits to a code. No invented options possible.
    "use_entropy_picker": True,
    "use_llm_question_wording": False,
    "question_wording_model": os.environ.get("QUESTION_WORDING_MODEL", os.environ.get("CLASSIFY_LLM_MODEL", "gpt-5-nano")),
    "retrieval": {                  # see local_db.retrieve_candidates
        # ---- Production defaults v2 (Exp 2+3 leg-ablation verdict) ----
        # Production AI labels: generated descriptions, synonyms, colloquial
        # terms and brands. These act like retrieval facts and are required for
        # trader-language demo prompts such as "pre workout drink powder".
        "use_labels": True,
        # Curated: HURTS top-10 on LOO+vague. Off.
        "use_curated": False,
        # Description vector: drags in semantically-similar-but-wrong codes
        # for vague queries. Removing it: +2.8pp @5, +10pp head@5 on
        # naive_vague. Off by default; flip on for A/B in Compare tab.
        "use_vector": False,
        # Structured workhorses: facts/KG FTS + per-fact/edge embeddings.
        # Removing any of these costs 3-5pp @5.
        "use_facts_leg": True,
        "use_kg_context_leg": True,
        "use_facts_vec_leg": True,
        "use_kg_vec_leg": True,
        # FTS caps stay at 0.5 (boost not dominate).
        "facts_cap": 0.5,
        "kg_cap": 0.5,
        # Semantic caps bumped from 0.6 -> 0.9: best MRR (0.320, +11%) and
        # best @10 (50%, +5.7pp). The semantic legs are doing real work and
        # were being underweighted at 0.6.
        "facts_vec_cap": 0.9,
        "kg_vec_cap": 0.9,
        "rrf_k": 60,
    },
    "kg_include": {                 # see local_db.kg_edges_for_candidates docstring
        "chapter_notes": True,
        "section_notes": True,
        "legacy_blob_notes": False,
        "girs": True,
        "atar_rationales": True,
        "heading_rules": True,
        "other_global": True,
    },
}

DATA_DIR = Path(__file__).parent / "data"


def _selected_candidate_model(config: Optional[dict] = None) -> str:
    cfg = config or {}
    selected = str(cfg.get("candidate_selection_model") or os.environ.get("CLASSIFY_LLM_MODEL") or "gpt-5-nano").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{2,80}", selected):
        return "gpt-5-nano"
    return selected


def _selected_question_model(config: Optional[dict] = None) -> str:
    cfg = config or {}
    selected = str(
        cfg.get("question_wording_model")
        or os.environ.get("QUESTION_WORDING_MODEL")
        or cfg.get("candidate_selection_model")
        or os.environ.get("CLASSIFY_LLM_MODEL")
        or "gpt-5-nano"
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{2,80}", selected):
        return "gpt-5-nano"
    return selected


def _use_llm_candidate_selection(config: Optional[dict] = None) -> bool:
    cfg = config or {}
    return bool(cfg.get("use_llm_candidate_selection", False)) and provider_calls_allowed()


def _question_wording_summary(config: Optional[dict] = None) -> dict:
    cfg = config or {}
    if not cfg.get("use_llm_question_wording"):
        return {"mode": "deterministic", "reason": "LLM question wording disabled by config."}
    if os.environ.get("JOURNEY_CLASSIFY_MODE", "").strip().lower() in {"deterministic", "offline"}:
        return {"mode": "deterministic", "reason": "Offline/deterministic classification mode is active."}
    if not openai_allowed():
        return {"mode": "deterministic", "reason": "Provider calls are not enabled."}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"mode": "deterministic", "reason": "OPENAI_API_KEY is not set."}
    return {"mode": "llm", "model": _selected_question_model(cfg)}


# TERMINOLOGY (the table name kg.commodity_facets is a historical misnomer):
#   - A FACET is an attribute axis (facet_key): material_upper, closure,
#     construction. It is only meaningful ACROSS a candidate shortlist.
#   - A FACT is one code's value on a facet - a single kg.commodity_facets row
#     (facet_key + facet_value + evidence for one commodity).
#   - A QUESTION is asked about one facet; its options are the facet's fact
#     vocabulary across the current shortlist; the trader's answer keeps the
#     candidates whose fact matches (codes with NO fact on that facet are kept,
#     absence of metadata is not evidence against).
#
# The real OTT AI Guided Search context template - copied verbatim from
# ai-fan-out, then AUGMENTED with the AI-440 layers:
#   - STRUCTURED_FACTS: the facts matrix (codes x facets) across the candidate set
#   - KG_EDGES: chapter/section notes, exclusions, ATAR rationales
# The LLM uses both to (a) pick a discriminating facet for the next question,
# (b) draw answer options from the facts present on that facet, (c) apply
# chapter rules.
CONTEXT_TEMPLATE = """You're an expert Harmonised System code classifier.

Look at the search input and any previously answered questions and decide whether more questions are needed to confidently assign a commodity code.

If answers are available, use them to help formulate your questions and answers - don't go beyond these search results in terms of the overall commodity hierarchy - even if you know the results are incorrect.

When STRUCTURED_FACTS are available for the candidate set, prefer to phrase the next question around a facet whose values DIFFER across candidates - and use the actual values present as options. Avoid asking about facets that all candidates share. When KG_EDGES are present, apply them as hard rules: if an edge says "this chapter does not cover X", do not propose X. If an edge specifies a classification order (e.g. "outer sole material first"), respect that order when asking.

## Response format

Respond in JSON format with one of the following:

### Confident answer

Rank the top 5 opensearch answers by confidence and provide the most likely answer if you are confident.

When you answer, ALWAYS return a ranked list of the top 5 candidate codes (or all surviving candidates if fewer than 5), never a single code - the trader reviews the ranked list and confirms the final choice.

    {
      "answers": [
        { "commodity_code": "0101210000", "confidence": "Strong" },
        { "commodity_code": "0101290000", "confidence": "Good" },
        { "commodity_code": "0101300000", "confidence": "Possible" }
      ]
    }

### Follow-up questions

Each question can have many possible answers. Try and ask as few questions as possible to narrow down the commodity code.

**AVOID YES/NO QUESTIONS** unless they will help narrow down the commodity code by whole categories - a user can review each opensearch option themselves and answer yes/no so yes/no just makes the UX worse.

Keep to one question per commodity code search.

    {
      "questions": [
        { "question": "What is the material of the clothing?", "options": ["Cotton", "Wool", "Synthetic"] }
      ]
    }

Prefer questions and options that will help you narrow down the commodity code the most and avoid repeating the same question.

Try and ask at least a few questions each time to narrow down the commodity code in an efficient way.

### Error

    {
      "error": "Contradictory answers given"
    }

## Rules

- Always respond in JSON as per the three examples above and never try and code anything up.
- Always structure questions so they have multiple meaningful options, not just yes/no.
- Avoid hallucinating codes and only provide codes that you are certain of based on the information provided.

## Context sections

-----------SEARCH_INPUT------------------
%{search_input}
-----------END SEARCH_INPUT--------------

-----------EXPANDED_QUERY-----------------
%{expanded_query}
-----------END EXPANDED_QUERY-------------

-----------ANSWERS_OPENSEARCH-------------
%{answers_opensearch}
-----------END ANSWERS_OPENSEARCH---------

-----------QUESTIONS_AND_ANSWERS----------
%{questions}
-----------END QUESTIONS_AND_ANSWERS------

-----------STRUCTURED_FACTS---------------
%{structured_facts}
-----------END STRUCTURED_FACTS-----------

-----------KG_EDGES-----------------------
%{kg_edges}
-----------END KG_EDGES-------------------
"""


@lru_cache(maxsize=1)
def load_facets_index() -> dict:
    return json.loads((DATA_DIR / "facets.json").read_text())["facets"]


@lru_cache(maxsize=1)
def load_kg_edges() -> list[dict]:
    return json.loads((DATA_DIR / "kg_edges.json").read_text())["edges"]


@lru_cache(maxsize=1)
def load_local_commodities() -> dict:
    """The slice of commodities we have hand-authored facets for.
    Returns dict keyed by dotted code AND flat code so lookups work either way.
    """
    raw = json.loads((DATA_DIR / "commodities.json").read_text())["commodities"]
    out: dict[str, dict] = {}
    for c in raw:
        out[c["code"]] = c
        flat = re.sub(r"\D", "", c["code"]).ljust(10, "0")[:10]
        out[flat] = c
    return out


# --- LLM ---------------------------------------------------------------

def _llm_expand_query(raw_query: str) -> Optional[str]:
    """AI-441 style: rewrite shorthand / trade names into normalised terms.

    Cheap pre-classify LLM call. Returns the expanded query or None on failure.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_allowed() or not api_key or not raw_query.strip():
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        system = (
            "You normalise UK trader product queries for a tariff classification search. "
            "Rewrite shorthand, brand names, abbreviations and trade jargon into the equivalent "
            "neutral descriptive terms a tariff database would use. "
            "Examples: 'AISI 316 bearings' -> 'stainless steel ball bearings (austenitic chromium-nickel-molybdenum)'. "
            "'fridge' -> 'household refrigerator'. 'iPhone 15' -> 'cellular smartphone'. "
            "Preserve all the trader's product details (size, colour, country if they mentioned it). "
            "Output ONLY the rewritten query - no preamble, no quotes, no commentary."
        )
        model = os.environ.get("CLASSIFY_LLM_MODEL", "gpt-5.5")
        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": raw_query},
            ],
        }
        _apply_llm_tuning(kwargs, model)
        r = client.chat.completions.create(**kwargs)
        text = (r.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        print(f"[query_expansion LLM] {type(e).__name__}: {e}")
        return None


# In-process memo of query-expansion results (item 17): repeated demos of the
# same first-turn query skip the expansion LLM call entirely. Keyed on
# (raw_query, expansion model, prompt variant); answer-turn expansions depend
# on qa_history so they are never cached. A lock around a dict is enough for
# uvicorn's default single-worker threading model.
_EXPANSION_CACHE_TTL_S = 600.0
_EXPANSION_CACHE_MAX = 200
_expansion_cache: dict[tuple[str, str, str], tuple[float, str]] = {}
_expansion_cache_lock = threading.Lock()


def _expansion_cache_get(key: tuple[str, str, str]) -> Optional[str]:
    with _expansion_cache_lock:
        hit = _expansion_cache.get(key)
        if hit is None:
            return None
        ts, value = hit
        if time.monotonic() - ts > _EXPANSION_CACHE_TTL_S:
            _expansion_cache.pop(key, None)
            return None
        return value


def _expansion_cache_put(key: tuple[str, str, str], value: str) -> None:
    now = time.monotonic()
    with _expansion_cache_lock:
        if key not in _expansion_cache and len(_expansion_cache) >= _EXPANSION_CACHE_MAX:
            # Drop expired entries first, then the oldest, to stay under the cap.
            for k in [k for k, (ts, _) in _expansion_cache.items() if now - ts > _EXPANSION_CACHE_TTL_S]:
                _expansion_cache.pop(k, None)
            while len(_expansion_cache) >= _EXPANSION_CACHE_MAX:
                oldest = min(_expansion_cache, key=lambda k: _expansion_cache[k][0])
                _expansion_cache.pop(oldest, None)
        _expansion_cache[key] = (now, value)


def _expand_query_for_config(raw_query: str, qa_history: list[dict], cfg: dict) -> Optional[str]:
    model = str(cfg.get("query_expansion_model") or os.environ.get("TRIAGE_MODEL") or "gpt-5-mini")
    prompt_variant = str(cfg.get("query_expansion_prompt_variant") or "mine")
    cache_key: Optional[tuple[str, str, str]] = (
        (raw_query.strip().lower(), model, prompt_variant) if not qa_history else None
    )
    if cache_key is not None:
        cached = _expansion_cache_get(cache_key)
        if cached is not None:
            return cached
    try:
        from . import triage
        result = triage.expand_query(
            raw_query,
            qa_history=qa_history or None,
            model=model,
            prompt_variant=prompt_variant,
        )
    except Exception as exc:
        print(f"[query_expansion staging] {type(exc).__name__}: {exc}")
        result = _llm_expand_query(raw_query)
    if cache_key is not None and result and str(result).strip():
        _expansion_cache_put(cache_key, result)
    return result


# ----- AI-440: symbolic info-gain + neural phrasing ---------------------

def _pick_facet_by_entropy(
    facet_lookup: dict[str, list[dict]],
    min_coverage: float = 0.25,
    min_distinct_values: int = 2,
    exclude_keys: Optional[set[str]] = None,
) -> tuple[Optional[str], list[str], float, dict]:
    """Server-side facet picker. No LLM involved.

    Returns (facet_key, options, entropy, debug). Options are the distinct
    facet values across the candidate set - guaranteed in-vocabulary.

    Selection rule:
      1. Drop facets with < min_coverage of candidates having any value.
      2. Drop facets with < min_distinct_values distinct values (can't disambiguate).
      3. Pick the facet with the highest Shannon entropy across its distribution.

    facet_lookup: {commodity_code: [{facet_key, facet_value, ...}, ...]}
    """
    import math
    from collections import defaultdict, Counter

    n_candidates = len(facet_lookup)
    if n_candidates == 0:
        return None, [], 0.0, {"reason": "no candidates"}

    # Aggregate values per facet_key. One value per (code, key) pair (max if multiple).
    # Track which codes have which keys for coverage calculation.
    key_to_values: dict[str, list[str]] = defaultdict(list)
    key_to_codes: dict[str, set[str]] = defaultdict(set)
    for code, facets in facet_lookup.items():
        for f in facets:
            key = f.get("facet_key")
            val = str(f.get("facet_value") or "").strip().lower()
            if not key or not val:
                continue
            # Item 7: after a 'None of these' answer the caller may exclude
            # already-asked facets so the next question covers a new aspect.
            if exclude_keys and key in exclude_keys:
                continue
            # Skip operational/commercial-context facets. They matter later for
            # declaration or duty, but they are hostile as classification Q&A.
            if not _facet_allows_scope(f, "qa"):
                continue
            key_to_values[key].append(val)
            key_to_codes[key].add(code)

    candidates_evaluated = []
    for key, values in key_to_values.items():
        coverage = len(key_to_codes[key]) / n_candidates
        if coverage < min_coverage:
            continue
        distinct = set(values)
        if len(distinct) < min_distinct_values:
            continue
        counts = Counter(values)
        total = sum(counts.values())
        entropy = -sum((c / total) * math.log2(c / total) for c in counts.values())
        gri_priority = _facet_question_priority(str(key))
        candidates_evaluated.append({
            "facet_key": key,
            "entropy": entropy,
            "coverage": coverage,
            "distinct_values": sorted(distinct),
            "value_counts": dict(counts),
            "gri_priority": gri_priority,
            "gri_priority_label": _facet_question_priority_label(gri_priority),
        })

    if not candidates_evaluated:
        return None, [], 0.0, {
            "reason": "no facet met coverage/distinct-values threshold",
            "n_candidates": n_candidates,
            "facets_seen": len(key_to_values),
        }

    # GIR-style ordering first, then entropy. The question should ask about the
    # legally/classificatorily decisive characteristic before duty context,
    # even if a lower-priority key has slightly higher entropy.
    candidates_evaluated.sort(key=lambda x: (x["gri_priority"], -x["entropy"], -x["coverage"]))
    winner = candidates_evaluated[0]
    debug = {
        "n_candidates": n_candidates,
        "n_facet_keys_considered": len(key_to_values),
        "top3_candidates": [
            {k: v for k, v in c.items() if k != "value_counts"}
            for c in candidates_evaluated[:3]
        ],
        "winner_value_counts": winner["value_counts"],
    }
    return winner["facet_key"], winner["distinct_values"], winner["entropy"], debug


def _llm_choose_action(
    raw_query: str,
    expanded_query: str,
    candidates: list[dict],
    qa_history: list[dict],
    session_facts: list[dict],
    facet_key: Optional[str],
    facet_options: list[str],
    structured_facts_text: str,
    kg_edges_text: str,
    prompt_mode: str = "baseline",
    kg_include: Optional[dict] = None,
    model: str | None = None,
    max_questions: int = 7,
) -> tuple[Optional[dict], dict]:
    """LLM either commits to codes from the candidate enum OR phrases the
    server-picked question. Uses OpenAI tool calling to enforce both
    constraints at the API level - no possibility of off-list codes or
    invented options.

    Returns (parsed_action, debug) where parsed_action looks like one of:
      {"mode": "answers", "answers": [{commodity_code, confidence}, ...]}
      {"mode": "questions", "question": str, "options": [str, ...], "facet_key": str}
      {"mode": "error", "error_message": str}
    """
    debug: dict = {}
    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_allowed() or not api_key:
        return None, debug
    try:
        from openai import OpenAI
    except Exception as e:
        debug["import_error"] = str(e)
        return None, debug

    # Candidate enum for the commit_answer tool. Capped at 50 to keep schema light.
    candidate_codes = [c["commodity_code"] for c in candidates[:50]]
    if not candidate_codes:
        return None, debug

    qa_text = "No previous questions." if not qa_history else "\n".join(
        f"Q{i}: {h['question']}\nA{i}: {h['answer']}"
        for i, h in enumerate(qa_history, 1)
    )

    sf_section = ""
    if session_facts:
        try:
            from . import session_facts as sf_mod
            sf_section = sf_mod.session_facts_prompt_section(session_facts)
        except Exception:
            sf_section = ""

    # Build per-candidate facet sheet so the LLM can decide if it can commit.
    candidate_sheet = "\n".join(
        f"{i}. {c['commodity_code']} - {c['description']} (score: {c['score']:.3f})"
        for i, c in enumerate(candidates[:50], 1)
    )

    system = (
        "You are a UK customs classification expert helping a trader pick the right "
        "10-digit commodity code. You see the candidate codes (with their facet sheets), "
        "the user's query, prior Q&A, and user-asserted facts.\n\n"
        "You MUST call exactly one of the provided tools:\n"
        "- commit_answer: when you can rank the most likely codes (use this if you have "
        "  enough information OR if no further question would discriminate).\n"
        "- ask_clarifying_question: when ONE more piece of trader information would "
        "  meaningfully narrow the candidate set. The server has already picked the "
        "  most discriminating facet for you - you just phrase the question naturally.\n\n"
        "Apply user-asserted facts as HARD CONSTRAINTS. Apply tier-1/2 KG rules as "
        "binding. Pick at most 5 codes for commit_answer, ranked by likelihood."
        + _prompt_mode_system_suffix(prompt_mode)
        + _question_depth_block(qa_history, max_questions)
    )

    candidate_codes_for_excl = [c["commodity_code"] for c in candidates[:50]]
    excl_block = (
        _exclusion_notes_block(candidate_codes_for_excl, include=kg_include)
        if prompt_mode == "exclusion_aware" else ""
    )

    user_content_parts = [
        f"## Raw user query\n{raw_query}",
        f"## Expanded query\n{expanded_query}" if expanded_query != raw_query else "",
        f"## User-asserted facts\n{sf_section}" if sf_section and sf_section != "(no user-asserted facts extracted)" else "",
        f"## Prior Q&A\n{qa_text}",
        f"## Candidate codes (your enum for commit_answer)\n{candidate_sheet}",
        f"## Structured facts across candidates\n{structured_facts_text}",
        f"## KG rules in scope\n{kg_edges_text}",
        excl_block,
    ]
    if facet_key:
        user_content_parts.append(
            f"## Server-selected discriminator\n"
            f"If you decide to ask, the facet to disambiguate is: **{facet_key}**\n"
            f"Available answer options (the only options the user can pick):\n" +
            "\n".join(f"  - {o}" for o in facet_options) +
            f"\nYou only need to phrase the question naturally for that facet."
        )
    else:
        user_content_parts.append(
            "## Server-selected discriminator\n"
            "No high-entropy facet available. You should commit_answer rather than ask."
        )
    user_prompt = "\n\n".join(p for p in user_content_parts if p)

    # Tool definitions - the API enforces the schema
    commit_tool = {
        "type": "function",
        "function": {
            "name": "commit_answer",
            "description": "Commit to a ranked list of likely commodity codes (max 5).",
            "parameters": {
                "type": "object",
                "properties": {
                    "answers": {
                        "type": "array",
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "commodity_code": {"type": "string", "enum": candidate_codes},
                                "confidence": {"type": "string", "enum": ["Strong", "Possible", "From retrieval"]},
                                "reasoning": {"type": "string"}
                            },
                            "required": ["commodity_code", "confidence"]
                        }
                    }
                },
                "required": ["answers"]
            }
        }
    }
    ask_tool = {
        "type": "function",
        "function": {
            "name": "ask_clarifying_question",
            "description": (
                f"Ask the trader a clarifying question about facet '{facet_key or '(none)'}'. "
                "Only phrase the question naturally. Do not invent answer options - the "
                "server has already chosen them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phrasing": {
                        "type": "string",
                        "description": "Natural-language question phrasing for the discriminator facet."
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this question narrows the candidate set."
                    }
                },
                "required": ["phrasing"]
            }
        }
    }
    tools = [commit_tool, ask_tool] if facet_key else [commit_tool]

    model = model or _selected_candidate_model()
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "tools": tools,
        "tool_choice": "required",  # MUST call one of the tools
    }
    debug["model"] = model
    _apply_llm_tuning(kwargs, model)
    debug["prompt_mode"] = prompt_mode

    try:
        import time
        client = OpenAI(api_key=api_key)
        t0 = time.time()
        resp = client.chat.completions.create(**kwargs)
        debug["latency_ms"] = int((time.time() - t0) * 1000)
    except Exception as e:
        debug["api_error"] = repr(e)
        return None, debug

    msg = resp.choices[0].message
    tool_calls = msg.tool_calls or []
    if not tool_calls:
        debug["no_tool_call"] = True
        return None, debug

    call = tool_calls[0]
    fn_name = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except Exception as e:
        debug["args_parse_error"] = str(e)
        return None, debug

    debug["tool_called"] = fn_name
    debug["prompt_chars"] = sum(len(m["content"]) for m in kwargs["messages"])

    if fn_name == "commit_answer":
        # Legacy shape: {"answers": [...]} so downstream code works unchanged.
        return {"answers": args.get("answers", [])}, debug

    if fn_name == "ask_clarifying_question":
        # Legacy shape: {"questions": [{"question": ..., "options": [...]}]}
        return {
            "questions": [{
                "question": args.get("phrasing", ""),
                "options": list(facet_options),
                "facet_key": facet_key,
                "rationale": args.get("rationale", ""),
            }]
        }, debug

    debug["unknown_tool"] = fn_name
    return None, debug


def _llm_classify(
    raw_query: str,
    candidates: list[dict],
    qa_history: list[dict],
    config: Optional[dict] = None,
    expanded_query: Optional[str] = None,
    session_facts: Optional[list[dict]] = None,
) -> tuple[Optional[dict], dict]:
    """Call the LLM with the OTT prompt + config-controlled augmentation.

    `expanded_query` is the AI-441-style rewrite computed UPSTREAM by
    `classify_step` so it can feed both retrieval and this prompt. If None,
    we fall back to the raw query and skip expansion - the caller is
    responsible for the expansion lifecycle.

    Returns (parsed_json | None, debug_info). debug_info carries the actual
    prompt sections so the comparison UI can show what changed.
    """
    cfg = {**DEFAULT_CLASSIFY_CONFIG, **(config or {})}
    debug: dict = {"config_applied": cfg}

    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_allowed() or not api_key:
        return None, debug
    try:
        from openai import OpenAI
    except Exception as e:
        print(f"[classify] openai import failed: {e}")
        return None, debug

    # Expansion is precomputed by classify_step (used to retrieve AND prompt).
    expanded = (expanded_query or raw_query).strip()
    if expanded and expanded != raw_query:
        debug["expanded_query"] = expanded

    opensearch_text = "\n".join(
        f"{i}. {c['commodity_code']} - {c['description']} (score: {c['score']:.3f})"
        for i, c in enumerate(candidates[:80], 1)
    )
    qa_text = "No previous questions." if not qa_history else "\n".join(
        f"Q{i}: {h['question']}\nA{i}: {h['answer']}"
        for i, h in enumerate(qa_history, 1)
    )

    codes = [c["commodity_code"] for c in candidates[:80]]
    use_kg_prompt_context = bool(cfg.get("use_kg_prompt_context", True))
    structured_facts_text = (
        _structured_facts_section(codes)
        if (use_kg_prompt_context and cfg.get("use_facets"))
        else "Disabled by config."
    )
    kg_edges_text = (
        _kg_edges_section(codes, include=cfg.get("kg_include"))
        if use_kg_prompt_context
        else "Disabled by config."
    )

    # Session-time facts: prepend to STRUCTURED_FACTS so the LLM sees them as
    # hard constraints from the trader. Also append a HARDENING line that
    # locks down question options to the facet vocabulary we actually have.
    if session_facts:
        try:
            from . import session_facts as sf_mod
            sf_section = sf_mod.session_facts_prompt_section(session_facts)
        except Exception:
            sf_section = ""
    else:
        sf_section = ""
    hardening = (
        "\n\n--- HARDENING ---\n"
        "When you ASK A QUESTION, every option you propose MUST be a value that "
        "actually appears in STRUCTURED_FACTS above for at least one candidate. "
        "Do not invent option text. Do not paraphrase a facet value. "
        "If a candidate facet says 'rubber', use 'rubber' verbatim, not 'flexible rubber'.\n"
        "Ask about ONE facet per question, and keep each option a SINGLE facet value - "
        "never combine two values into one option with '+' or 'and'.\n"
        "Phrase the question itself in plain English that a non-expert trader understands. "
        "Never use internal facet key names in the question (ask 'What is the top part of "
        "the footwear made of?', not 'What is the material_upper?'). If a tariff term is "
        "unavoidable, add a short plain-English explanation in brackets.\n"
        "When you give an ANSWER, choose only from the candidate codes listed in OPENSEARCH RESULTS."
    )
    if sf_section and sf_section != "(no user-asserted facts extracted)":
        structured_facts_text = (
            f"USER-ASSERTED FACTS (treat as hard constraints):\n{sf_section}\n\n"
            f"{structured_facts_text}{hardening}"
        )
    else:
        structured_facts_text = f"{structured_facts_text}{hardening}"

    prompt = (
        CONTEXT_TEMPLATE
        .replace("%{search_input}", raw_query)
        .replace("%{expanded_query}", expanded)
        .replace("%{answers_opensearch}", opensearch_text)
        .replace("%{questions}", qa_text)
        .replace("%{structured_facts}", structured_facts_text)
        .replace("%{kg_edges}", kg_edges_text)
    )
    # prompt_mode scaffolding (additive; baseline = no change)
    prompt_mode = cfg.get("prompt_mode", "baseline")
    prompt += _prompt_mode_system_suffix(prompt_mode)
    if prompt_mode == "exclusion_aware":
        prompt += _exclusion_notes_block(codes, include=cfg.get("kg_include"))
    # Trader-journey UX: the interactive journey sends first_turn_must_ask so
    # the trader always confirms key facts before a code is recommended. Eval
    # and compare callers do not send it, so their semantics are unchanged.
    if not qa_history and cfg.get("first_turn_must_ask"):
        prompt += (
            "\n\n--- FIRST TURN RULE ---\n"
            "No questions have been asked yet, and the trader expects to confirm "
            "key facts before a code is recommended. You MUST respond with the "
            "\"questions\" JSON shape on this turn: ONE question, with options "
            "drawn from STRUCTURED_FACTS values where available. Do NOT return "
            "\"answers\" on this first turn. On later turns keep asking one "
            "question at a time until a high-confidence ranked list is possible "
            f"(hard cap {int(cfg.get('max_questions', 7))} questions in total)."
        )
    # Confidence-driven question depth (items 8+13): keep asking while the
    # ranked list is not yet high-confidence, never past the hard cap
    # (cfg max_questions, default 7). The server additionally coerces any
    # question returned past the cap into a ranked answer list.
    prompt += _question_depth_block(qa_history, int(cfg.get("max_questions", 7)))
    debug["prompt_mode"] = prompt_mode
    debug["prompt_chars"] = len(prompt)
    debug["sections"] = {
        "structured_facts_chars": len(structured_facts_text),
        "kg_edges_chars": len(kg_edges_text),
        "opensearch_chars": len(opensearch_text),
        "qa_chars": len(qa_text),
        "session_facts_count": len(session_facts or []),
        "kg_prompt_context_enabled": use_kg_prompt_context,
    }

    client = OpenAI(api_key=api_key)
    model = _selected_candidate_model(cfg)
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Classify: {raw_query}"},
        ],
        "response_format": {"type": "json_object"},
    }
    debug["model"] = model
    _apply_llm_tuning(kwargs, model)

    try:
        import time
        t0 = time.time()
        resp = client.chat.completions.create(**kwargs)
        debug["latency_ms"] = int((time.time() - t0) * 1000)
        text = (resp.choices[0].message.content or "").strip()
        return json.loads(text), debug
    except Exception as e:
        print(f"[classify LLM] {type(e).__name__}: {e}")
        return None, debug


# --- Prompt augmentation: facets + KG edges -----------------------------

_TIER_LABELS = {
    1: "BINDING-LEGAL-RULE",
    2: "BINDING-RULING",
    3: "AUTHORITATIVE-GUIDANCE",
    4: "CURATED-EXPERT",
    5: "AI-DERIVED-FROM-AUTHORITATIVE",
    6: "AI-DERIVED-FROM-DESCRIPTION",
    7: "FOOTNOTE-METADATA",
    8: "EXTERNAL",
}


def _kg_edges_section(codes: list[str], include: Optional[dict] = None) -> str:
    if not codes:
        return "No KG edges available."
    edges = [e for e in db_kg_edges(codes, include=include) if _edge_allows_scope(e, "classification")]
    if not edges:
        return "No KG edges applicable to this candidate set (with current config)."
    lines = [
        "Knowledge-graph rules applicable to this candidate set, sorted by authority tier (lower = more binding).",
        "Apply tier 1-2 rules as HARD CONSTRAINTS. Tier 3-4 are strong evidence. Tier 5-7 are softer hints.",
    ]

    # Sort by (authority_tier asc, scope_narrowness asc) - most binding + most specific first.
    def _scope_rank(e):
        s = e.get("scope") or ""
        if s.startswith("heading:"): return 0
        if s.startswith("chapter:"): return 1
        if s.startswith("section:"): return 2
        return 3
    edges_sorted = sorted(
        edges,
        key=lambda e: (int(e.get("authority_tier") or 8), _scope_rank(e)),
    )
    for e in edges_sorted[:15]:
        body = (e.get("body") or "")[:600]
        tier = int(e.get("authority_tier") or 8)
        tier_label = _TIER_LABELS.get(tier, "?")
        lines.append(f"\n  [T{tier} {tier_label} | {e.get('scope','?')}] {e.get('title','?')}")
        lines.append(f"    {body}")
        lines.append(f"    (source: {e.get('source','?')})")
    if len(edges_sorted) > 15:
        lines.append(f"\n  ... plus {len(edges_sorted)-15} more edges not shown for brevity")
    return "\n".join(lines)


def _structured_facts_section(codes: list[str]) -> str:
    """Render a per-candidate facet matrix as a compact, readable text block.

    Format:
      code               | facet_key       | values present in this code
      6402.20.00         | material_upper  | rubber_or_plastic
                         | closure         | strap_thong
      6402.99.31         | material_upper  | rubber_or_plastic
                         | closure         | strap_buckle

    Only includes candidates that actually have facets - keeps the prompt small.
    """
    if not codes:
        return "No structured facts available for the candidate set."
    by_code = db_facets_for_codes(codes)
    populated = {c: facets for c, facets in by_code.items() if facets}
    if not populated:
        return "No structured facts available for the candidate set."

    # Group by code, then by facet_key (a code can have multiple values per key)
    lines: list[str] = []
    lines.append(
        "Classification-safe facet matrix across the candidate set "
        "(operational, duty, document, trade-geo, and Search References alias facts omitted):"
    )
    for code in codes:
        if code not in populated:
            continue
        facets = populated[code]
        by_key: dict[str, list[str]] = {}
        for f in facets:
            key = str(f.get("facet_key") or "")
            if not _facet_allows_scope(f, "classification"):
                continue
            by_key.setdefault(key, []).append(str(f["facet_value"]))
        if not by_key:
            continue
        lines.append(f"\n  {code}:")
        for key, values in by_key.items():
            # Dedupe while preserving order
            uniq = list(dict.fromkeys(values))
            lines.append(f"    - {key}: {', '.join(uniq)}")

    # Cross-candidate summary: which facets actually differ?
    all_keys: dict[str, set[str]] = {}
    for code, facets in populated.items():
        for f in facets:
            key = str(f.get("facet_key") or "")
            if not _facet_allows_scope(f, "classification"):
                continue
            all_keys.setdefault(key, set()).add(str(f["facet_value"]))
    discriminating = {k: vs for k, vs in all_keys.items() if len(vs) >= 2}
    if discriminating:
        lines.append("\nFacets that DIFFER across the candidate set (good question candidates):")
        for k, vs in sorted(discriminating.items(), key=lambda x: -len(x[1])):
            lines.append(f"  - {k}: values seen = {sorted(vs)}")
    return "\n".join(lines)


# --- Frozen pool, question depth, confidence + leaf policies ------------
#
# Items 3a/6/7/8/9/11/13 of the approved trader-journey backlog. Shared by
# the converge (classify_step) and eliminate paths.

POOL_CAP = 100  # frozen retrieval pool cap (item 9)

NONE_OF_THESE_OPTION = "None of these / not sure"


def _is_none_answer(answer: str) -> bool:
    """True when the trader picked the standing 'None of these / not sure'
    option (or an equivalent older none-option wording)."""
    text = _normal_match(str(answer or ""))
    if not text:
        return False
    return "none of these" in text or text in {"none", "not sure", "unsure"}


def _with_none_option(options: list[str]) -> list[str]:
    """Item 7: every question turn carries a standing escape option."""
    out = [str(o) for o in options if str(o).strip()]
    if not any("none of these" in str(o).lower() for o in out):
        out.append(NONE_OF_THESE_OPTION)
    return out


def _question_depth_block(qa_history: list[dict], cap: int) -> str:
    """Items 7+8+13 prompt scaffolding: confidence-driven question depth with
    a hard cap, plus different-aspect guidance after a 'None of these' answer."""
    asked = len(qa_history or [])
    parts = [
        "\n\n--- QUESTION DEPTH POLICY ---\n"
        f"Questions asked so far: {asked} of a HARD CAP of {cap}.\n"
        "Keep asking ONE clarifying question at a time until you can produce a "
        "HIGH-CONFIDENCE ranked answer list: the top-ranked code is a clear best "
        "match AND the top two ranked codes fall within the same 6-digit "
        "subheading (or only one plausible code remains). Never exceed the cap."
    ]
    if asked >= cap:
        parts.append(
            f"\nTHE CAP IS REACHED ({asked} answered). Do NOT ask another question. "
            "Return your ranked answers now - if the goods remain ambiguous, return "
            "an honest shortlist of the surviving candidates anyway."
        )
    if qa_history and _is_none_answer(str(qa_history[-1].get("answer") or "")):
        prev_q = str(qa_history[-1].get("question") or "").strip()
        parts.append(
            "\nThe trader answered 'None of these / not sure' to the previous question"
            + (f' ("{prev_q}")' if prev_q else "")
            + ". Do NOT rule out or eliminate any candidate because of that answer. "
            "You are FORBIDDEN from asking about the same property again, even "
            "reworded or with finer-grained options - if you asked about a material, "
            "do not ask about that material in any form. Ask about a genuinely "
            "different characteristic (construction, closure, intended use, sole "
            "material, who it is for, how it is packaged...). If no OTHER "
            "discriminating characteristic exists across the candidates, return "
            "ranked answers now instead of asking again."
        )
    return "".join(parts)


def _db_probe_state() -> str:
    """Resolve the tariff-DB health probe to 'live' | 'fixture' | 'down'.

    Contract: local_db gains a TTL probe `live_db_health() -> str`; until it
    lands we fall back to `_live_db_available()`. Both lookups are guarded
    with getattr so this module never hard-depends on either symbol.
    """
    probe = getattr(_local_db, "live_db_health", None)
    if callable(probe):
        try:
            state = str(probe() or "").strip().lower()
        except Exception:
            state = ""
        if state in {"live", "ok", "healthy", "up"}:
            return "live"
        if state.startswith("fixture"):
            return "fixture"
        if state:
            return "down"
    available = getattr(_local_db, "_live_db_available", None)
    try:
        if callable(available) and available():
            return "live"
    except Exception:
        pass
    return "fixture"


def _turn_retrieval_health(candidates: list[dict], llm_ok: bool = True) -> str:
    """Item 11 label for a turn: 'live' | 'fixture' | 'degraded' | 'infra-error'.

    - live: DB probe healthy (a genuine zero-match keeps 'live')
    - degraded: retrieval worked but no LLM (provider off/unavailable/failed)
    - fixture: serving the bundled fixture data
    - infra-error: retrieval came back empty BECAUSE the DB is unreachable
    """
    state = _db_probe_state()
    if state == "live":
        return "live" if llm_ok else "degraded"
    if state == "fixture":
        return "fixture"
    return "fixture" if candidates else "infra-error"


def _db_retrieve_with_cfg(query_text: str, retrieval_cfg: dict, limit: Optional[int] = None) -> list[dict]:
    """db_retrieve with the standard retrieval-config kwargs applied."""
    return db_retrieve(
        query_text,
        limit=int(limit if limit is not None else retrieval_cfg.get("limit", 40)),
        use_curated=retrieval_cfg.get("use_curated", False),
        use_labels=retrieval_cfg.get("use_labels", True),
        use_vector=retrieval_cfg.get("use_vector", False),
        use_composite=retrieval_cfg.get("use_composite", False),
        use_facts=retrieval_cfg.get("use_facts_leg", True),
        use_kg_context=retrieval_cfg.get("use_kg_context_leg", True),
        use_facts_vec=retrieval_cfg.get("use_facts_vec_leg", True),
        use_kg_vec=retrieval_cfg.get("use_kg_vec_leg", True),
        facts_cap=float(retrieval_cfg.get("facts_cap", 0.5)),
        kg_cap=float(retrieval_cfg.get("kg_cap", 0.5)),
        facts_vec_cap=float(retrieval_cfg.get("facts_vec_cap", 0.9)),
        kg_vec_cap=float(retrieval_cfg.get("kg_vec_cap", 0.9)),
        rrf_k=int(retrieval_cfg.get("rrf_k", 60)),
        exclude_fact_sources=retrieval_cfg.get("exclude_fact_sources"),
        exclude_edge_ids=retrieval_cfg.get("exclude_edge_ids"),
        use_vec_adapter=retrieval_cfg.get("use_vec_adapter", False),
    )


def _union_candidate_pools(
    base: list[dict],
    extra: list[dict],
    cap: int = POOL_CAP,
    mark_new_source: Optional[str] = None,
) -> list[dict]:
    """Item 9: union two retrieval pools, deduped by code (keeping the better
    fused score and merged sources), sorted by score, capped."""
    by_code: dict[str, dict] = {}
    for c in base:
        code = str(c.get("commodity_code") or "")
        if code:
            by_code[code] = dict(c)
    for c in extra:
        code = str(c.get("commodity_code") or "")
        if not code:
            continue
        if code in by_code:
            row = by_code[code]
            row["score"] = max(float(row.get("score") or 0.0), float(c.get("score") or 0.0))
            row["sources"] = sorted(set(row.get("sources") or []) | set(c.get("sources") or []))
        else:
            row = dict(c)
            if mark_new_source:
                row["sources"] = sorted(set(row.get("sources") or []) | {mark_new_source})
            by_code[code] = row
    pooled = sorted(by_code.values(), key=lambda r: float(r.get("score") or 0.0), reverse=True)
    return pooled[:cap]


def _pool_answer_matches(pool: list[dict], answer: str) -> int:
    """How many frozen-pool candidates the trader's latest answer plausibly
    matches (facet values, plain option label, or description tokens)."""
    if not str(answer or "").strip() or not pool:
        return 0
    try:
        facet_lookup = db_facets_for_codes([c["commodity_code"] for c in pool])
    except Exception:
        facet_lookup = {}
    matches = 0
    for c in pool:
        try:
            if _candidate_matches_qa_answer(c, answer, facet_lookup):
                matches += 1
        except Exception:
            continue
    return matches


def _rescue_pool(
    query: str,
    qa_history: list[dict],
    cfg: dict,
    retrieval_cfg: dict,
    pool: list[dict],
) -> tuple[list[dict], dict]:
    """Item 9 rescue search: the trader's answers stopped matching the frozen
    pool (or they picked 'None of these'). Re-expand the query WITH the Q&A
    history, retrieve once, and union the new hits into the pool (deduped,
    capped, new hits marked source 'rescue'). The pool is never shrunk below
    its surviving members, so presence is protected."""
    rescue_query = None
    try:
        rescue_query = _expand_query_for_config(query, qa_history, cfg)
    except Exception as exc:
        print(f"[classify rescue] expansion failed: {exc!r}")
    if not rescue_query or not rescue_query.strip():
        answers_text = " ".join(
            str(h.get("answer") or "")
            for h in qa_history
            if not _is_none_answer(str(h.get("answer") or ""))
        )
        rescue_query = f"{query} {answers_text}".strip()
    before = {c["commodity_code"] for c in pool}
    try:
        hits = _db_retrieve_with_cfg(rescue_query, retrieval_cfg)
    except Exception as exc:
        print(f"[classify rescue] retrieval failed: {exc!r}")
        hits = []
    merged = _union_candidate_pools(pool, hits, mark_new_source="rescue")
    return merged, {
        "triggered": True,
        "rescue_query": rescue_query,
        "new_hits": len({c["commodity_code"] for c in merged} - before),
        "pool_size": len(merged),
    }


_CONFIDENCE_RANKS = {
    "strong": 0,
    "best match": 0,
    "good": 1,
    "possible": 2,
    "also possible": 2,
    "from facts": 2,
    "from retrieval": 3,
}


def _confidence_rank(value: Any) -> int:
    return _CONFIDENCE_RANKS.get(str(value or "").strip().lower(), 2)


def _top_answer_dominates(answers: list[dict]) -> bool:
    """Items 6/8 dominance test for the top-ranked answer: it is the only
    answer, the top two agree at 6-digit subheading level, or its raw
    confidence outranks the runner-up's."""
    if not answers:
        return False
    if len(answers) == 1:
        return True
    top6 = str(answers[0].get("commodity_code") or "")[:6]
    second6 = str(answers[1].get("commodity_code") or "")[:6]
    if top6 and top6 == second6:
        return True
    return _confidence_rank(answers[0].get("confidence")) < _confidence_rank(answers[1].get("confidence"))


def _apply_confidence_policy(answers: list[dict]) -> list[dict]:
    """Item 6: normalise every emitted confidence to the public enum -
    'Best match' (top slot, dominant only) | 'Also possible' | 'From retrieval'.
    Legacy values map Strong->Best match, Good/Possible->Also possible."""
    if not answers:
        return answers
    dominant = _top_answer_dominates(answers)
    out = []
    for i, a in enumerate(answers):
        raw = str(a.get("confidence") or "").strip().lower()
        if raw == "from retrieval":
            conf = "From retrieval"
        elif i == 0 and dominant:
            conf = "Best match"
        else:
            conf = "Also possible"
        out.append({**a, "confidence": conf})
    return out


def _answers_high_confidence(answers: list[dict]) -> bool:
    """Item 8 'high-confidence' encoding: top answer is 'Best match' AND the
    top two ranked codes share the 6-digit subheading (or only one remains)."""
    if not answers or answers[0].get("confidence") != "Best match":
        return False
    if len(answers) == 1:
        return True
    return str(answers[0].get("commodity_code") or "")[:6] == str(answers[1].get("commodity_code") or "")[:6]


def _is_declarable_leaf_safe(code: str) -> bool:
    """Contract wrapper for local_db.is_declarable_leaf - safe fallback True
    when the helper or the DB is unavailable."""
    fn = getattr(_local_db, "is_declarable_leaf", None)
    if not callable(fn):
        return True
    try:
        return bool(fn(code))
    except Exception:
        return True


def _declarable_leaf_children_safe(code: str) -> list[dict]:
    """Contract wrapper for local_db.declarable_leaf_children - safe fallback
    [] when the helper or the DB is unavailable."""
    fn = getattr(_local_db, "declarable_leaf_children", None)
    if not callable(fn):
        return []
    try:
        return list(fn(code) or [])
    except Exception:
        return []


_LEAF_STOPWORDS = {"the", "and", "for", "with", "other", "not", "than", "from", "containing"}


def _implied_leaf_children(children: list[dict], answer_tokens: set[str]) -> list[dict]:
    """Children whose description tokens overlap the trader's Q&A answers."""
    if not answer_tokens:
        return []
    matched = []
    for child in children:
        tokens = {
            t for t in _normal_match(str(child.get("description") or "")).split()
            if len(t) >= 3 and t not in _LEAF_STOPWORDS
        }
        if tokens & answer_tokens:
            matched.append(child)
    return matched


def _leaf_adjust_answers(
    answers: list[dict],
    qa_history: list[dict],
    query: str = "",
) -> tuple[list[dict], dict]:
    """Item 3a: ensure the emitted ranked list points at live declarable leaves.

    - A non-declarable code uniquely implied to one child by the Q&A facts (or
      with exactly one live child) is SUBSTITUTED by that child (leaf_adjusted).
    - Otherwise the parent is kept but its top 2 declarable children are
      appended as extra 'Also possible' answers, so a declarable option always
      exists - and a declarable answer is promoted into the top slot if the
      top entry is still non-declarable.
    Safe no-op when the leaf helpers/DB are unavailable.
    """
    if not answers:
        return answers, {"applied": False}
    leaf_cache: dict[str, bool] = {}

    def _is_leaf(code: str) -> bool:
        if code not in leaf_cache:
            leaf_cache[code] = _is_declarable_leaf_safe(code)
        return leaf_cache[code]

    answer_text = " ".join(
        [str(query or "")]
        + [
            str(h.get("answer") or "")
            for h in (qa_history or [])
            if not _is_none_answer(str(h.get("answer") or ""))
        ]
    )
    answer_tokens = {
        t for t in _normal_match(answer_text).split()
        if len(t) >= 3 and t not in _LEAF_STOPWORDS
    }

    adjusted: list[dict] = []
    seen: set[str] = set()
    trace: list[dict] = []

    def _push(entry: dict) -> None:
        code = str(entry.get("commodity_code") or "")
        if code and code not in seen:
            seen.add(code)
            adjusted.append(entry)

    for a in answers:
        code = str(a.get("commodity_code") or "")
        if not code or _is_leaf(code):
            _push(a)
            continue
        children = _declarable_leaf_children_safe(code)
        if not children:
            _push(a)
            trace.append({"code": code, "action": "kept_no_declarable_children"})
            continue
        implied = _implied_leaf_children(children, answer_tokens)
        chosen = implied[0] if len(implied) == 1 else (children[0] if len(children) == 1 else None)
        if chosen:
            child_code = str(chosen.get("commodity_code") or "")
            leaf_cache.setdefault(child_code, True)
            _push({
                **a,
                "commodity_code": child_code,
                "description": str(chosen.get("description") or "") or str(a.get("description") or ""),
                "leaf_adjusted": True,
            })
            trace.append({
                "code": code,
                "action": "descended_to_leaf",
                "leaf": child_code,
                "via": "qa_implied" if len(implied) == 1 else "only_child",
            })
            continue
        # Ambiguous: keep the parent but surface its top declarable children.
        _push(a)
        added = []
        for child in children[:2]:
            child_code = str(child.get("commodity_code") or "")
            leaf_cache.setdefault(child_code, True)
            _push({
                "commodity_code": child_code,
                "confidence": "Also possible",
                "description": str(child.get("description") or ""),
                "leaf_adjusted": True,
            })
            added.append(child_code)
        trace.append({"code": code, "action": "kept_parent_appended_leaves", "leaves": added})

    # Never leave a non-declarable code in the top slot while a declarable
    # alternative exists in the list.
    if adjusted and not _is_leaf(str(adjusted[0].get("commodity_code") or "")):
        promote_idx = next(
            (i for i, a in enumerate(adjusted) if _is_leaf(str(a.get("commodity_code") or ""))),
            None,
        )
        if promote_idx is not None:
            adjusted.insert(0, adjusted.pop(promote_idx))
            # The demoted entry must not keep the 'Best match' top label.
            adjusted = [
                {**a, "confidence": "Also possible"}
                if i > 0 and a.get("confidence") == "Best match" else a
                for i, a in enumerate(adjusted)
            ]
            trace.append({
                "action": "promoted_declarable_to_top",
                "code": adjusted[0].get("commodity_code"),
            })
    return adjusted, {"applied": bool(trace), "trace": trace}


# --- Driver ------------------------------------------------------------

def _emit_progress(
    on_progress: Optional[Callable[[str, dict], None]],
    event: str,
    payload: dict,
) -> None:
    """Best-effort milestone callback (item 17 SSE streaming). Never raises."""
    if on_progress is None:
        return
    try:
        on_progress(event, payload)
    except Exception:
        pass


def classify_step(
    query: str,
    qa_history: list[dict],
    config: Optional[dict] = None,
    fixed_candidates: Optional[list[dict]] = None,
    on_progress: Optional[Callable[[str, dict], None]] = None,
) -> dict:
    cfg = {**DEFAULT_CLASSIFY_CONFIG, **(config or {})}
    retrieval_cfg = {**DEFAULT_CLASSIFY_CONFIG["retrieval"], **(cfg.get("retrieval") or {})}
    """Run one turn of the AI Guided Search loop.

    `on_progress(event, payload)` is an optional best-effort milestone callback
    (item 17 SSE streaming): expansion_done -> retrieval_done ->
    candidates_ready -> llm_started -> turn_complete. Behaviour is unchanged
    when it is None.

    Returns a dict with:
      - candidates: list of {commodity_code, description, score, sources}
      - fixed_candidates: frozen retrieval pool to echo back on later turns
      - retrieval_health: 'live' | 'fixture' | 'degraded' | 'infra-error'
      - llm_response: parsed LLM JSON (or None)
      - mode: "answers" | "questions" | "error" | "no_candidates"
      - answers: parsed answers list (when mode==answers)
      - question: the single question we surface (when mode==questions)
      - kg_notes: any KG edges relevant to the in-slice candidates
      - facet_enrichment: facets dict for the top in-slice candidate (if any)

    Item 9 (freeze + rescue): turn 1 retrieves with BOTH the expanded and the
    raw query and freezes the deduped union as the candidate pool. When the
    caller echoes that pool back (fixed_candidates) on later turns we do NOT
    re-retrieve - the pool only widens via the rescue search when the trader's
    answers stop matching it (or they pick 'None of these / not sure').
    """
    # Query expansion (AI-441): runs BEFORE retrieval so the rewritten query
    # feeds both legs of the system. Previously expansion only fed the LLM
    # prompt - retrieval still used the raw vague query. That was a bug.
    # Expand only on the first turn (qa_history empty); later turns already
    # have refining context from Q&A so re-expansion would muddy intent.
    expanded_query = query
    if cfg.get("use_query_expansion") and _use_llm_candidate_selection(cfg) and not qa_history:
        ex = _expand_query_for_config(query, qa_history, cfg)
        if ex and ex.strip() and ex.strip() != query.strip():
            expanded_query = ex.strip()
    _emit_progress(on_progress, "expansion_done", {"expanded_query": expanded_query})

    # Session-time fact extraction: pull structured facts from the user's
    # input + qa_history so they can be used as a re-ranker on the candidate
    # list AND as HARD CONSTRAINTS in the classification prompt. Closes the
    # "user volunteers info we don't have facets for" gap. 'None of these'
    # answers are skipped - they assert nothing about the goods (item 7).
    session_facts: list[dict] = []
    if cfg.get("use_session_facts"):
        try:
            from . import session_facts as sf_mod
            sf_history = [h for h in qa_history if not _is_none_answer(str(h.get("answer") or ""))]
            session_facts = sf_mod.extract_session_facts(query, sf_history)
        except Exception as exc:
            print(f"[classify] session_facts extraction failed: {exc!r}")

    rescue_debug: Optional[dict] = None
    if qa_history and fixed_candidates:
        # Later turn with a frozen pool: never re-retrieve. Rescue only widens.
        candidates = [dict(c) for c in fixed_candidates]
        latest_answer = str(qa_history[-1].get("answer") or "")
        survivor_matches = _pool_answer_matches(candidates, latest_answer)
        if _is_none_answer(latest_answer) or survivor_matches < 3:
            candidates, rescue_debug = _rescue_pool(query, qa_history, cfg, retrieval_cfg, candidates)
            rescue_debug["reason"] = (
                "none_of_these" if _is_none_answer(latest_answer) else "answers_stopped_matching"
            )
            rescue_debug["survivor_matches"] = survivor_matches
    else:
        # Turn 1 (or a caller that does not echo the pool back): retrieve with
        # the expanded AND the raw query, union + dedupe into the frozen pool.
        candidates = _db_retrieve_with_cfg(expanded_query, retrieval_cfg)
        if expanded_query.strip() != query.strip():
            candidates = _union_candidate_pools(candidates, _db_retrieve_with_cfg(query, retrieval_cfg))
        else:
            candidates = candidates[:POOL_CAP]
    # Snapshot the pool BEFORE per-turn re-ranking: the pool identity stays
    # stable across turns and is returned for the caller to echo back.
    frozen_pool = [dict(c) for c in candidates]
    _emit_progress(on_progress, "retrieval_done", {
        "count": len(candidates),
        "pool_size": len(frozen_pool),
    })
    if not candidates:
        _emit_progress(on_progress, "turn_complete", {})
        return {
            "candidates": [],
            "fixed_candidates": frozen_pool,
            "retrieval_health": _turn_retrieval_health([], llm_ok=True),
            "llm_response": None,
            "mode": "no_candidates",
            "answers": [],
            "question": None,
            "kg_notes": [],
            "facet_enrichment": None,
            "qa_history": qa_history,
            "session_facts": session_facts,
        }

    # Apply session-fact re-ranker: boost candidates whose stored facets
    # match the user-asserted facts, penalise contradictions.
    if session_facts:
        try:
            from . import session_facts as sf_mod
            candidates = sf_mod.rerank_with_session_facts(candidates, session_facts)
        except Exception as exc:
            print(f"[classify] session_facts rerank failed: {exc!r}")
    if not candidates:
        # Presence protection: a turn must never end up with an empty pool.
        candidates = [dict(c) for c in frozen_pool]

    if on_progress is not None:
        try:
            preview = _enrich_candidates(candidates[:10])
        except Exception:
            preview = candidates[:10]
        _emit_progress(on_progress, "candidates_ready", {
            "candidates": [
                {
                    "commodity_code": str(c.get("commodity_code") or ""),
                    "description": str(c.get("description") or ""),
                    "sources": list(c.get("sources") or []),
                }
                for c in preview
            ],
            "fixed_candidates_size": len(frozen_pool),
        })

    # AI-440 path: server picks facet by entropy, LLM picks via function-calling.
    # Falls through to legacy LLM-only path if disabled or no good facet exists.
    if not _use_llm_candidate_selection(cfg) or os.environ.get("JOURNEY_CLASSIFY_MODE", "").strip().lower() in {"deterministic", "offline"}:
        parsed, debug = None, {
            "config_applied": cfg,
            "candidate_selection": {
                "mode": "deterministic",
                "reason": "LLM candidate selection disabled by config or offline mode.",
            },
        }
    elif cfg.get("use_entropy_picker"):
        cand_codes_for_facets = [c["commodity_code"] for c in candidates[:80]]
        facet_lookup = db_facets_for_codes(cand_codes_for_facets) if cfg.get("use_facets") else {}
        # Item 7: after a 'None of these' answer the next question must cover
        # a DIFFERENT aspect - exclude facets already asked about.
        exclude_facets: Optional[set[str]] = None
        if qa_history and _is_none_answer(str(qa_history[-1].get("answer") or "")):
            asked_keys = {str(h.get("facet_key")) for h in qa_history if h.get("facet_key")}
            exclude_facets = asked_keys or None
        chosen_key, chosen_options, entropy, ep_debug = _pick_facet_by_entropy(
            facet_lookup, exclude_keys=exclude_facets,
        )
        use_kg_prompt_context = bool(cfg.get("use_kg_prompt_context", True))
        structured_facts_text = (
            _structured_facts_section(cand_codes_for_facets)
            if (use_kg_prompt_context and cfg.get("use_facets")) else "Disabled by config."
        )
        kg_edges_text = (
            _kg_edges_section(cand_codes_for_facets, include=cfg.get("kg_include"))
            if use_kg_prompt_context else "Disabled by config."
        )
        _emit_progress(on_progress, "llm_started", {"model": _selected_candidate_model(cfg)})
        parsed, debug = _llm_choose_action(
            raw_query=query,
            expanded_query=expanded_query,
            candidates=candidates,
            qa_history=qa_history,
            session_facts=session_facts,
            facet_key=chosen_key,
            facet_options=chosen_options,
            structured_facts_text=structured_facts_text,
            kg_edges_text=kg_edges_text,
            prompt_mode=cfg.get("prompt_mode", "baseline"),
            kg_include=cfg.get("kg_include"),
            model=_selected_candidate_model(cfg),
            max_questions=int(cfg.get("max_questions", 7)),
        )
        if debug is None:
            debug = {}
        debug["entropy_picker"] = {
            "chosen_facet_key": chosen_key,
            "chosen_options": chosen_options,
            "entropy": entropy,
            **ep_debug,
        }
        debug["config_applied"] = cfg
    else:
        _emit_progress(on_progress, "llm_started", {"model": _selected_candidate_model(cfg)})
        parsed, debug = _llm_classify(
            query, candidates, qa_history,
            config=config, expanded_query=expanded_query,
            session_facts=session_facts,
        )
    parsed = parsed or {}

    # Items 8+13 server-side enforcement: NEVER return another question past
    # the cap - coerce to a ranked shortlist built from the current survivors.
    # (No first-turn coercion: qa_history must be non-empty.)
    question_cap = int(cfg.get("max_questions", 7))
    question_cap_coerced = False
    if parsed.get("questions") and qa_history and len(qa_history) >= question_cap:
        debug["question_cap_coercion"] = {
            "cap": question_cap,
            "original_questions": parsed.get("questions"),
        }
        parsed = {
            "answers": [
                {"commodity_code": c["commodity_code"], "confidence": "Also possible"}
                for c in candidates[:5]
            ]
        }
        question_cap_coerced = True

    candidates_out = _enrich_candidates(candidates)

    # Track which retrieval legs contributed - useful for the Compare panel
    legs_used = sorted({s for c in candidates for s in c.get("sources", [])})
    debug["retrieval_legs"] = legs_used

    # Augmentation summary for the UI - reflects what the config actually applied.
    cand_codes = [c["commodity_code"] for c in candidates]
    facet_lookup = db_facets_for_codes(cand_codes) if cfg.get("use_facets") else {}
    facets_count = sum(1 for fs in facet_lookup.values() if fs)
    kg_edges_applied = db_kg_edges(cand_codes, include=cfg.get("kg_include"))
    augmentation_summary = {
        "qa_process_mode": cfg.get("qa_process_mode"),
        "kg_prompt_context_enabled": bool(cfg.get("use_kg_prompt_context", True)),
        "candidates_with_facets": facets_count,
        "total_candidates": len(candidates),
        "kg_edges_applied": len(kg_edges_applied),
        "candidate_selection": debug.get("candidate_selection") or {
            "mode": "llm",
            "model": debug.get("model") or _selected_candidate_model(cfg),
        },
        "question_wording": _question_wording_summary(cfg),
        "session_facts": session_facts,
        "session_facts_count": len(session_facts),
        "question_cap": question_cap,
        "question_cap_coerced": question_cap_coerced,
        "frozen_pool_size": len(frozen_pool),
        "rescue": rescue_debug,
        "debug": debug,  # prompt sizes, applied config, latency, expanded query
    }

    if "answers" in parsed and isinstance(parsed["answers"], list):
        answers = parsed["answers"]
        # Make sure each answer carries a description from the candidate set.
        cand_map = {c["commodity_code"]: c for c in candidates_out}
        enriched_answers = []
        for a in answers:
            code = a.get("commodity_code")
            cand = cand_map.get(code)
            enriched_answers.append({
                "commodity_code": code,
                "confidence": a.get("confidence", "Unknown"),
                "description": (cand or {}).get("description", ""),
            })
        # self_verify mode: a 2nd LLM pass may reorder (never shrink) the list.
        if cfg.get("prompt_mode") == "self_verify":
            enriched_answers, sv_debug = self_verify_answers(
                query, qa_history, enriched_answers, candidates_out, kg_include=cfg.get("kg_include"),
            )
            augmentation_summary["self_verify"] = sv_debug
        # Item 6: public confidence enum. Item 3a: declarable-leaf filtering.
        enriched_answers = _apply_confidence_policy(enriched_answers)
        enriched_answers, leaf_debug = _leaf_adjust_answers(enriched_answers, qa_history, query)
        augmentation_summary["leaf_adjustment"] = leaf_debug
        augmentation_summary["high_confidence"] = _answers_high_confidence(enriched_answers)
        top = enriched_answers[0] if enriched_answers else None
        top_code = top["commodity_code"] if top else None
        _emit_progress(on_progress, "turn_complete", {})
        return {
            "candidates": candidates_out,
            "fixed_candidates": frozen_pool,
            "retrieval_health": _turn_retrieval_health(candidates_out, llm_ok=True),
            "llm_response": parsed,
            "mode": "answers",
            "answers": enriched_answers,
            "question": None,
            "kg_notes": _kg_notes_for(top_code) if top_code else [],
            "facet_enrichment": _facets_for(top_code) if top_code else None,
            "augmentation_summary": augmentation_summary,
            "qa_history": qa_history,
        }

    if "questions" in parsed and parsed["questions"]:
        q = parsed["questions"][0]
        options_raw = list(q.get("options", []))
        # Phase-2 hardening: post-validate each option against the real facet
        # vocabulary across the candidate set. We don't reject - just flag in
        # debug so trust + reproducibility are auditable. The HARDENING block
        # in the prompt already biases the LLM strongly toward facet values.
        all_facet_values: set[str] = set()
        for code, facets in facet_lookup.items():
            for f in facets:
                v = str(f.get("facet_value") or "").strip().lower()
                if v:
                    all_facet_values.add(v)
        validated = []
        off_vocab_count = 0
        for opt in options_raw:
            o = str(opt).strip()
            lo = o.lower()
            # Exact, substring, or token-overlap match counts as "drawn from facet vocab"
            from_vocab = (
                lo in all_facet_values
                or any(v in lo or lo in v for v in all_facet_values if len(v) >= 4)
            )
            validated.append({"text": o, "from_facet_vocab": bool(from_vocab)})
            if not from_vocab:
                off_vocab_count += 1
        debug["question_options_total"] = len(options_raw)
        debug["question_options_off_vocab"] = off_vocab_count
        # Item 7: standing escape option on every question turn. Appended
        # AFTER validation, pre-marked in-vocab so it is never flagged.
        if not any("none of these" in v["text"].lower() for v in validated):
            validated.append({"text": NONE_OF_THESE_OPTION, "from_facet_vocab": True, "standing": True})
        _emit_progress(on_progress, "turn_complete", {})
        return {
            "candidates": candidates_out,
            "fixed_candidates": frozen_pool,
            "retrieval_health": _turn_retrieval_health(candidates_out, llm_ok=True),
            "llm_response": parsed,
            "mode": "questions",
            "answers": [],
            "question": {
                "question": q.get("question", ""),
                "options": [v["text"] for v in validated],
                "options_validated": validated,  # per-option vocab flag for trace
                "off_vocab_count": off_vocab_count,
            },
            "kg_notes": _kg_notes_for_candidates(candidates_out),
            "facet_enrichment": None,
            "augmentation_summary": augmentation_summary,
            "qa_history": qa_history,
        }

    if "error" in parsed:
        _emit_progress(on_progress, "turn_complete", {})
        return {
            "candidates": candidates_out,
            "fixed_candidates": frozen_pool,
            "retrieval_health": _turn_retrieval_health(candidates_out, llm_ok=True),
            "llm_response": parsed,
            "mode": "error",
            "answers": [],
            "question": None,
            "kg_notes": [],
            "facet_enrichment": None,
            "augmentation_summary": augmentation_summary,
            "qa_history": qa_history,
            "error_message": parsed["error"],
        }

    # LLM unavailable - degrade by returning the top-5 retrieval hits as candidates
    # and letting the trader pick one manually.
    fallback_answers = [
        {
            "commodity_code": c["commodity_code"],
            "confidence": "From retrieval",
            "description": c["description"],
        }
        for c in candidates_out[:5]
    ]
    fallback_answers, leaf_debug = _leaf_adjust_answers(fallback_answers, qa_history, query)
    augmentation_summary["leaf_adjustment"] = leaf_debug
    _emit_progress(on_progress, "turn_complete", {})
    return {
        "candidates": candidates_out,
        "fixed_candidates": frozen_pool,
        "retrieval_health": _turn_retrieval_health(candidates_out, llm_ok=False),
        "llm_response": None,
        "mode": "answers",
        "answers": fallback_answers,
        "question": None,
        "kg_notes": [],
        "facet_enrichment": None,
        "augmentation_summary": augmentation_summary,
        "qa_history": qa_history,
    }


# --- ELIMINATE strategy -------------------------------------------------
#
# The disambiguation analogue of the converge flow. Converge RE-RETRIEVES and
# RE-COMMITS every round, which a diagnostic showed drops ~43% of retrievable
# golds (a gold present at round 1 falls out of the top-N after a later
# re-retrieve). ELIMINATE fixes the candidate set at round 1 and NEVER
# re-retrieves: each round it uses the accumulated facts/answers ONLY to rule
# OUT candidates that are definitively excluded, and presents the surviving set
# RANKED by confidence. A gold that was retrievable at round 1 can therefore
# only be lost if the LLM positively rules it out - presence is monotonically
# protected.


def initial_candidates_for_eliminate(
    query: str,
    config: Optional[dict] = None,
    candidate_limit: int = 40,
) -> tuple[list[dict], list[dict]]:
    """Round-1 retrieval for the eliminate strategy: retrieve + session-fact
    re-rank ONCE, return the frozen candidate set (raw + enriched) the loop will
    carry for the rest of the session. Mirrors the retrieval leg of classify_step
    but does no LLM classify call.
    """
    cfg = {**DEFAULT_CLASSIFY_CONFIG, **(config or {})}
    retrieval_cfg = {**DEFAULT_CLASSIFY_CONFIG["retrieval"], **(cfg.get("retrieval") or {})}

    expanded_query = query
    if (
        cfg.get("use_query_expansion")
        and _use_llm_candidate_selection(cfg)
        and cfg.get("qa_mode", "ask_first") == "answers_only"
    ):
        ex = _expand_query_for_config(query, [], cfg)
        if ex and ex.strip() and ex.strip() != query.strip():
            expanded_query = ex.strip()

    candidates = db_retrieve(
        expanded_query,
        limit=candidate_limit,
        use_curated=retrieval_cfg.get("use_curated", False),
        use_labels=retrieval_cfg.get("use_labels", True),
        use_vector=retrieval_cfg.get("use_vector", False),
        use_composite=retrieval_cfg.get("use_composite", False),
        use_facts=retrieval_cfg.get("use_facts_leg", True),
        use_kg_context=retrieval_cfg.get("use_kg_context_leg", True),
        use_facts_vec=retrieval_cfg.get("use_facts_vec_leg", True),
        use_kg_vec=retrieval_cfg.get("use_kg_vec_leg", True),
        facts_cap=float(retrieval_cfg.get("facts_cap", 0.5)),
        kg_cap=float(retrieval_cfg.get("kg_cap", 0.5)),
        facts_vec_cap=float(retrieval_cfg.get("facts_vec_cap", 0.9)),
        kg_vec_cap=float(retrieval_cfg.get("kg_vec_cap", 0.9)),
        rrf_k=int(retrieval_cfg.get("rrf_k", 60)),
        exclude_fact_sources=retrieval_cfg.get("exclude_fact_sources"),
        exclude_edge_ids=retrieval_cfg.get("exclude_edge_ids"),
        use_vec_adapter=retrieval_cfg.get("use_vec_adapter", False),
    )
    return candidates, _enrich_candidates(candidates)


def eliminate_step(
    query: str,
    qa_history: list[dict],
    fixed_candidates: list[dict],
    config: Optional[dict] = None,
) -> dict:
    """One round of the ELIMINATE flow over a FROZEN candidate set.

    Never retrieves. Uses query + qa_history (+ extracted session facts) to ask
    the LLM to (a) RULE OUT only candidates definitively excluded by the facts
    and KG rules, (b) RANK the survivors by confidence, and (c) optionally ask
    ONE more clarifying question if a single fact would let it rule out more.

    Returns the SAME result shape as classify_step so qa_loop can treat both
    strategies identically:
      mode in {"questions","answers","no_candidates","error"}
      candidates: enriched FROZEN set (unchanged across rounds)
      answers: survivors ranked (when mode=="answers")
      question: {question, options} (when mode=="questions")
      augmentation_summary, eliminate_trace
    """
    cfg = {**DEFAULT_CLASSIFY_CONFIG, **(config or {})}
    prompt_mode = cfg.get("prompt_mode", "baseline")
    candidates_out = _enrich_candidates(fixed_candidates)

    if not fixed_candidates:
        return {
            "candidates": [], "llm_response": None, "mode": "no_candidates",
            "retrieval_health": _turn_retrieval_health([], llm_ok=True),
            "answers": [], "question": None, "kg_notes": [], "facet_enrichment": None,
            "qa_history": qa_history, "augmentation_summary": {"strategy": "eliminate"},
        }

    # Session-fact extraction (same gap-closer the converge path uses).
    session_facts: list[dict] = []
    if cfg.get("use_session_facts"):
        try:
            from . import session_facts as sf_mod
            session_facts = sf_mod.extract_session_facts(query, qa_history)
        except Exception as exc:
            print(f"[eliminate] session_facts extraction failed: {exc!r}")

    codes = [c["commodity_code"] for c in fixed_candidates]
    structured_facts_text = (
        _structured_facts_section(codes) if cfg.get("use_facets") else "Disabled by config."
    )
    kg_edges_text = _kg_edges_section(codes, include=cfg.get("kg_include"))

    # Round-1 question is now LLM-authored (gpt-5.5) with full KG + shortlist
    # context, not deterministic - forced below via require_question.

    if (
        not _use_llm_candidate_selection(cfg)
        or os.environ.get("JOURNEY_CLASSIFY_MODE", "").strip().lower() in {"deterministic", "offline"}
        or not openai_allowed()
    ):
        deterministic = _deterministic_eliminate_turn(
            query=query,
            qa_history=qa_history,
            fixed_candidates=fixed_candidates,
            candidates_out=candidates_out,
            cfg=cfg,
            session_facts=session_facts,
        )
        if deterministic:
            return deterministic

    parsed, debug = _llm_eliminate(
        raw_query=query,
        candidates=fixed_candidates,
        qa_history=qa_history,
        session_facts=session_facts,
        structured_facts_text=structured_facts_text,
        kg_edges_text=kg_edges_text,
        prompt_mode=prompt_mode,
        kg_include=cfg.get("kg_include"),
        model=_selected_candidate_model(cfg),
        max_questions=int(cfg.get("max_questions", 7)),
        require_question=(not qa_history and cfg.get("qa_mode", "ask_first") != "answers_only"),
    )
    parsed = parsed or {}

    cand_map = {c["commodity_code"]: c for c in candidates_out}
    survivors_raw = parsed.get("survivors") or []
    ruled_out = parsed.get("ruled_out") or []

    # Build the ranked survivor answer list. Defensive: keep only in-set codes,
    # and if the LLM returned nothing usable, fall back to the frozen order so a
    # round never silently empties the set (presence protection).
    survivors: list[dict] = []
    seen: set[str] = set()
    for s in survivors_raw:
        code = s.get("commodity_code")
        if code in cand_map and code not in seen:
            seen.add(code)
            survivors.append({
                "commodity_code": code,
                "confidence": s.get("confidence", "Possible"),
                "description": cand_map[code].get("description", ""),
                "reasoning": s.get("reasoning", ""),
            })
    if not survivors:
        survivors = [
            {"commodity_code": c["commodity_code"], "confidence": "From retrieval",
             "description": c.get("description", ""), "reasoning": ""}
            for c in candidates_out
        ]

    augmentation_summary = {
        "strategy": "eliminate",
        "candidate_selection": {
            "mode": "llm",
            "model": debug.get("model") or _selected_candidate_model(cfg),
        },
        "question_wording": _question_wording_summary(cfg),
        "frozen_candidate_count": len(fixed_candidates),
        "survivor_count": len(survivors),
        "ruled_out_count": len(ruled_out),
        "session_facts": session_facts,
        "session_facts_count": len(session_facts),
        "debug": debug,
    }

    # Did the LLM ask for one more fact? Only honour it if options are present
    # and the trader is still under the question cap (items 8+13).
    q = parsed.get("question")
    if (
        isinstance(q, dict)
        and q.get("question")
        and q.get("options")
        and len(qa_history) < int(cfg.get("max_questions", 7))
    ):
        return {
            "candidates": candidates_out,
            "retrieval_health": _turn_retrieval_health(candidates_out, llm_ok=True),
            "llm_response": parsed,
            "mode": "questions",
            "answers": [],
            "question": {
                "question": q.get("question", ""),
                "options": _with_none_option(list(q.get("options", []))),
            },
            "kg_notes": _kg_notes_for_candidates(candidates_out),
            "facet_enrichment": None,
            "augmentation_summary": augmentation_summary,
            "qa_history": qa_history,
            "eliminate_trace": {"survivors": survivors, "ruled_out": ruled_out},
        }

    # self_verify: 2nd pass may reorder the surviving set (never shrink it).
    if prompt_mode == "self_verify":
        survivors, sv_debug = self_verify_answers(
            query, qa_history, survivors, candidates_out, kg_include=cfg.get("kg_include"),
        )
        augmentation_summary["self_verify"] = sv_debug

    # Items 6 + 3a on the emitted ranked list (full survivor set unchanged).
    ranked_answers = _apply_confidence_policy(survivors[:5])
    ranked_answers, leaf_debug = _leaf_adjust_answers(ranked_answers, qa_history, query)
    augmentation_summary["leaf_adjustment"] = leaf_debug
    augmentation_summary["high_confidence"] = _answers_high_confidence(ranked_answers)
    top_code = ranked_answers[0]["commodity_code"] if ranked_answers else None
    return {
        "candidates": candidates_out,
        "retrieval_health": _turn_retrieval_health(candidates_out, llm_ok=bool(parsed)),
        "llm_response": parsed,
        "mode": "answers",
        "answers": ranked_answers,
        "survivors_all": survivors,  # full ranked surviving set (>5 allowed)
        "question": None,
        "kg_notes": _kg_notes_for(top_code) if top_code else [],
        "facet_enrichment": _facets_for(top_code) if top_code else None,
        "augmentation_summary": augmentation_summary,
        "qa_history": qa_history,
        "eliminate_trace": {"survivors": survivors, "ruled_out": ruled_out},
    }


def _deterministic_eliminate_turn(
    query: str,
    qa_history: list[dict],
    fixed_candidates: list[dict],
    candidates_out: list[dict],
    cfg: dict,
    session_facts: list[dict],
    force_question: bool = False,
) -> Optional[dict]:
    """Offline fallback for the full product app.

    It still demonstrates the Q&A shape by asking the highest-entropy facet
    question from the bundled fact slice, then filtering candidates by the
    trader's answer. The LLM path remains preferred whenever a key is present.
    """
    codes = [c["commodity_code"] for c in fixed_candidates]
    facet_lookup = db_facets_for_codes(codes) if cfg.get("use_facets") else {}
    # Provider availability decides the item-11 health label: a by-design
    # deterministic first question with the LLM enabled stays 'live'; pure
    # offline/deterministic operation reports 'degraded'.
    llm_on = (
        _use_llm_candidate_selection(cfg)
        and os.environ.get("JOURNEY_CLASSIFY_MODE", "").strip().lower() not in {"deterministic", "offline"}
    )

    def summary(extra_debug: dict | None = None, count: int | None = None) -> dict:
        kg_edges = db_kg_edges(codes, include=cfg.get("kg_include"))
        return {
            "strategy": "eliminate",
            "fallback": "deterministic",
            "candidate_selection": {
                "mode": "deterministic",
                "reason": "LLM candidate selection disabled, unavailable, or offline mode.",
            },
            "question_wording": _question_wording_summary(cfg),
            "frozen_candidate_count": len(fixed_candidates),
            "survivor_count": count if count is not None else len(fixed_candidates),
            "ruled_out_count": 0,
            "session_facts": session_facts,
            "session_facts_count": len(session_facts),
            "candidates_with_facets": sum(1 for fs in facet_lookup.values() if fs),
            "total_candidates": len(fixed_candidates),
            "kg_edges_applied": len(kg_edges),
            "debug": extra_debug or {},
        }

    if qa_history:
        answer = str(qa_history[-1].get("answer") or "").strip().lower()
        matched_codes: set[str] = set()
        enriched_by_code = {c["commodity_code"]: c for c in candidates_out}
        for c in fixed_candidates:
            code = c["commodity_code"]
            description = str(c.get("description") or "").strip().lower()
            if description and (answer == description or answer in description or description in answer):
                matched_codes.add(code)
            plain_label = _candidate_plain_option(enriched_by_code.get(code) or c).lower()
            if plain_label and (
                answer == plain_label
                or answer in plain_label
                or plain_label in answer
                or _normal_match(answer) in _normal_match(plain_label)
            ):
                matched_codes.add(code)
            facets = facet_lookup.get(code) or facet_lookup.get(_to_dotted(code)) or []
            for f in facets:
                if not _facet_allows_scope(f, "qa"):
                    continue
                value = str(f.get("facet_value") or "").strip().lower()
                display_value = _smooth_option_value(value).lower()
                if value and (
                    answer == value
                    or answer in value
                    or value in answer
                    or answer == display_value
                    or _normal_match(answer) in _normal_match(display_value)
                    or _normal_match(display_value) in _normal_match(answer)
                ):
                    matched_codes.add(code)
        filtered = [c for c in fixed_candidates if c["commodity_code"] in matched_codes] if matched_codes else fixed_candidates
        filtered_out = _enrich_candidates(filtered)
        answers = [
            {
                "commodity_code": c["commodity_code"],
                "confidence": "From facts" if c["commodity_code"] in matched_codes else "From retrieval",
                "description": c.get("description", ""),
            }
            for c in filtered_out[:5]
        ]
        answers = _apply_confidence_policy(answers)
        answers, leaf_debug = _leaf_adjust_answers(answers, qa_history, query)
        top = answers[0]["commodity_code"] if answers else None
        return {
            "candidates": filtered_out,
            "fixed_candidates": fixed_candidates,
            "retrieval_health": _turn_retrieval_health(filtered_out, llm_ok=llm_on),
            "llm_response": None,
            "mode": "answers",
            "answers": answers,
            "question": None,
            "kg_notes": _kg_notes_for(top) if top else [],
            "facet_enrichment": _facets_for(top) if top else None,
            "augmentation_summary": summary(
                {"answer_matched_codes": sorted(matched_codes), "leaf_adjustment": leaf_debug},
                len(filtered),
            ),
            "qa_history": qa_history,
            "eliminate_trace": {"survivors": answers, "ruled_out": []},
        }

    top_chapter = fixed_candidates[0]["commodity_code"][:2] if fixed_candidates else ""
    question_codes = [
        c["commodity_code"]
        for c in fixed_candidates
        if c.get("commodity_code", "").startswith(top_chapter)
    ]
    question_lookup = (
        {code: facet_lookup.get(code) or facet_lookup.get(_to_dotted(code)) or [] for code in question_codes}
        if len(question_codes) >= 2
        else facet_lookup
    )
    chosen_key, chosen_options, entropy, debug = _pick_facet_by_entropy(question_lookup)
    if chosen_key and chosen_options:
        label = chosen_key.replace("_", " ")
        try:
            defs = {d["key"]: d for d in db_facet_defs([chosen_key])}
            label = defs.get(chosen_key, {}).get("label") or label
        except Exception:
            pass
        question_text, display_options = _smooth_facet_question(chosen_key, chosen_options, label, cfg=cfg)
        return {
            "candidates": candidates_out,
            "fixed_candidates": fixed_candidates,
            "retrieval_health": _turn_retrieval_health(candidates_out, llm_ok=llm_on),
            "llm_response": None,
            "mode": "questions",
            "answers": [],
            "question": {
                "question": question_text,
                "options": _with_none_option(display_options[:6]),
            },
            "kg_notes": _kg_notes_for_candidates(candidates_out),
            "facet_enrichment": None,
            "augmentation_summary": summary({
                "chosen_facet_key": chosen_key,
                "chosen_options": chosen_options,
                "display_options": display_options,
                "entropy": entropy,
                "question_scope": f"chapter:{top_chapter}" if len(question_codes) >= 2 else "all_candidates",
                **debug,
            }),
            "qa_history": qa_history,
            "eliminate_trace": {"survivors": candidates_out, "ruled_out": []},
        }

    if force_question:
        question_hint = smooth_candidate_question(
            fixed_candidates,
            use_llm=_llm_question_wording_enabled(cfg),
            model=_selected_question_model(cfg),
        )
        options = question_hint["options"]
        return {
            "candidates": candidates_out,
            "fixed_candidates": fixed_candidates,
            "retrieval_health": _turn_retrieval_health(candidates_out, llm_ok=llm_on),
            "llm_response": None,
            "mode": "questions",
            "answers": [],
            "question": {
                "question": question_hint["question"],
                "options": _with_none_option(options),
            },
            "kg_notes": _kg_notes_for_candidates(candidates_out),
            "facet_enrichment": None,
            "augmentation_summary": summary({
                "reason": "forced_first_question_no_discriminating_facet",
                "question_scope": "top_candidates",
                "chosen_options": options,
            }),
            "qa_history": qa_history,
            "eliminate_trace": {"survivors": candidates_out, "ruled_out": []},
        }

    answers = [
        {
            "commodity_code": c["commodity_code"],
            "confidence": "From retrieval",
            "description": c.get("description", ""),
        }
        for c in candidates_out[:5]
    ]
    answers = _apply_confidence_policy(answers)
    answers, leaf_debug = _leaf_adjust_answers(answers, qa_history, query)
    top = answers[0]["commodity_code"] if answers else None
    return {
        "candidates": candidates_out,
        "fixed_candidates": fixed_candidates,
        "retrieval_health": _turn_retrieval_health(candidates_out, llm_ok=llm_on),
        "llm_response": None,
        "mode": "answers",
        "answers": answers,
        "question": None,
        "kg_notes": _kg_notes_for(top) if top else [],
        "facet_enrichment": _facets_for(top) if top else None,
        "augmentation_summary": summary({"reason": "no discriminating facet", "leaf_adjustment": leaf_debug}),
        "qa_history": qa_history,
        "eliminate_trace": {"survivors": answers, "ruled_out": []},
    }


def _llm_eliminate(
    raw_query: str,
    candidates: list[dict],
    qa_history: list[dict],
    session_facts: list[dict],
    structured_facts_text: str,
    kg_edges_text: str,
    prompt_mode: str = "baseline",
    kg_include: Optional[dict] = None,
    model: str | None = None,
    max_questions: int = 7,
    require_question: bool = False,
) -> tuple[Optional[dict], dict]:
    """LLM pass for one eliminate round. Tool-calling enforces that survivors
    and ruled-out codes come from the FROZEN candidate enum - the model cannot
    introduce a code that wasn't retrieved at round 1.
    """
    debug: dict = {"prompt_mode": prompt_mode}
    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_allowed() or not api_key:
        return None, debug
    try:
        from openai import OpenAI
    except Exception as e:
        debug["import_error"] = str(e)
        return None, debug

    candidate_codes = [c["commodity_code"] for c in candidates]
    if not candidate_codes:
        return None, debug

    qa_text = "No previous questions." if not qa_history else "\n".join(
        f"Q{i}: {h['question']}\nA{i}: {h['answer']}"
        for i, h in enumerate(qa_history, 1)
    )
    sf_section = ""
    if session_facts:
        try:
            from . import session_facts as sf_mod
            sf_section = sf_mod.session_facts_prompt_section(session_facts)
        except Exception:
            sf_section = ""
    candidate_sheet = "\n".join(
        f"{i}. {c['commodity_code']} - {c['description']} (score: {c['score']:.3f})"
        for i, c in enumerate(candidates, 1)
    )

    system = (
        "You are a UK customs classification expert running an ELIMINATION round. "
        "You are given a FIXED shortlist of candidate commodity codes that will NOT change. "
        "Your job is NOT to re-search. Using the trader's query, the prior Q&A, and the "
        "user-asserted facts, you must:\n"
        "  1. RULE OUT only the candidates that are DEFINITIVELY excluded by the facts or by "
        "     a binding KG/section/chapter rule. If a candidate is merely less likely but still "
        "     possible, KEEP it - do not rule out on weak evidence.\n"
        "  2. RANK every SURVIVING candidate by confidence (most likely first).\n"
        "You MUST call the submit_elimination tool. Both survivors[].commodity_code and "
        "ruled_out[].commodity_code MUST come from the provided candidate list. "
        "Every candidate should appear in EXACTLY ONE of survivors or ruled_out. "
        "Optionally include a single clarifying question (with concrete options) ONLY if one "
        "more answer would let you rule out more candidates."
        + _prompt_mode_system_suffix(prompt_mode)
        + _question_depth_block(qa_history, max_questions)
    )
    if require_question:
        system += (
            "\n\nMANDATORY THIS ROUND: ask exactly ONE clarifying question and do NOT finalise. "
            "Pick the single question that best splits the current shortlist, grounded in where the "
            "candidates genuinely differ per the structured facts and KG rules above. Give 3-6 concrete, "
            "mutually-exclusive options taken from the real candidate distinctions (never yes/no). "
            "Put it in the tool `question` field with its options."
        )

    excl_block = (
        _exclusion_notes_block(candidate_codes, include=kg_include)
        if prompt_mode == "exclusion_aware" else ""
    )
    user_parts = [
        f"## Raw user query\n{raw_query}",
        f"## User-asserted facts\n{sf_section}" if sf_section and sf_section != "(no user-asserted facts extracted)" else "",
        f"## Prior Q&A\n{qa_text}",
        f"## FIXED candidate shortlist (your enum)\n{candidate_sheet}",
        f"## Structured facts across candidates\n{structured_facts_text}",
        f"## KG rules in scope\n{kg_edges_text}",
        excl_block,
    ]
    user_prompt = "\n\n".join(p for p in user_parts if p)
    debug["prompt_system"] = system
    debug["prompt_user"] = user_prompt[:3000] + ("\n...[truncated for display]" if len(user_prompt) > 3000 else "")

    tool = {
        "type": "function",
        "function": {
            "name": "submit_elimination",
            "description": "Submit the surviving (ranked) and ruled-out candidates for this round.",
            "parameters": {
                "type": "object",
                "properties": {
                    "survivors": {
                        "type": "array",
                        "description": "Surviving candidates, MOST LIKELY FIRST.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "commodity_code": {"type": "string", "enum": candidate_codes},
                                "confidence": {"type": "string", "enum": ["Strong", "Good", "Possible"]},
                                "reasoning": {"type": "string"},
                            },
                            "required": ["commodity_code", "confidence"],
                        },
                    },
                    "ruled_out": {
                        "type": "array",
                        "description": "Candidates DEFINITIVELY excluded this round.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "commodity_code": {"type": "string", "enum": candidate_codes},
                                "reason": {"type": "string"},
                            },
                            "required": ["commodity_code", "reason"],
                        },
                    },
                    "question": {
                        "type": "object",
                        "description": "Optional single clarifying question.",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "required": ["survivors"],
            },
        },
    }

    if require_question:
        tool["function"]["parameters"]["required"] = ["survivors", "question"]
        tool["function"]["parameters"]["properties"]["question"]["required"] = ["question", "options"]
        tool["function"]["parameters"]["properties"]["question"]["description"] = (
            "REQUIRED this round: one clarifying question with 3-6 concrete options."
        )

    model = model or _selected_candidate_model()
    debug["model"] = model

    # gpt-5.5 rejects chat.completions + reasoning_effort + function tools (400);
    # route it through the /v1/responses API instead. Every other model keeps
    # the chat.completions path unchanged.
    if model.startswith("gpt-5.5"):
        import time
        # Responses-API tool format is FLAT (no nested "function" wrapper).
        # Reuse the SAME parameters dict, incl. the require_question mutations.
        RT = {"type": "function", **tool["function"]}
        try:
            client = OpenAI(api_key=api_key)
            t0 = time.time()
            resp = client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                tools=[RT],
                tool_choice={"type": "function", "name": "submit_elimination"},
                reasoning={"effort": os.environ.get("CLASSIFY_REASONING_EFFORT", "high")},
                timeout=300,
            )
            debug["latency_ms"] = int((time.time() - t0) * 1000)
            debug["api"] = "responses"
            debug["prompt_chars"] = len(system) + len(user_prompt)
        except Exception as e:
            debug["api_error"] = repr(e)
            debug["api"] = "responses"
            return None, debug

        args = None
        for item in resp.output:
            if getattr(item, "type", None) == "function_call" and getattr(item, "name", None) == "submit_elimination":
                try:
                    args = json.loads(item.arguments or "{}")
                except Exception as e:
                    debug["args_parse_error"] = str(e)
                    return None, debug
                break
        if args is None:
            debug["no_tool_call"] = True
            return None, debug
        return args, debug

    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "tools": [tool],
        "tool_choice": "required",
    }
    _apply_llm_tuning(kwargs, model)

    try:
        import time
        client = OpenAI(api_key=api_key)
        t0 = time.time()
        resp = client.chat.completions.create(**kwargs)
        debug["latency_ms"] = int((time.time() - t0) * 1000)
        debug["prompt_chars"] = sum(len(m["content"]) for m in kwargs["messages"])
    except Exception as e:
        debug["api_error"] = repr(e)
        return None, debug

    tool_calls = resp.choices[0].message.tool_calls or []
    if not tool_calls:
        debug["no_tool_call"] = True
        return None, debug
    try:
        args = json.loads(tool_calls[0].function.arguments or "{}")
    except Exception as e:
        debug["args_parse_error"] = str(e)
        return None, debug
    return args, debug


def self_verify_answers(
    raw_query: str,
    qa_history: list[dict],
    answers: list[dict],
    candidates: list[dict],
    kg_include: Optional[dict] = None,
) -> tuple[list[dict], dict]:
    """prompt_mode='self_verify' second pass.

    A separate LLM call audits the committed ranked answer list against the
    in-scope EXCLUSION notes and can OVERTURN it (re-order, demote an excluded
    branch). It may only RE-ORDER the existing answers - it cannot add codes -
    so presence of the gold in the set is never destroyed by verification.

    Returns (possibly_reordered_answers, debug). On any failure the original
    answers are returned unchanged.
    """
    debug: dict = {"applied": False}
    if not answers or len(answers) < 2:
        return answers, debug  # nothing to re-order
    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_allowed() or not api_key:
        return answers, debug
    try:
        from openai import OpenAI
    except Exception as e:
        debug["import_error"] = str(e)
        return answers, debug

    codes = [a["commodity_code"] for a in answers]
    cand_map = {c["commodity_code"]: c for c in candidates}
    excl_block = _exclusion_notes_block(codes, include=kg_include)
    qa_text = "No previous questions." if not qa_history else "\n".join(
        f"Q{i}: {h['question']}\nA{i}: {h['answer']}" for i, h in enumerate(qa_history, 1)
    )
    sheet = "\n".join(
        f"{i}. {c} - {cand_map.get(c, {}).get('description', '')}" for i, c in enumerate(codes, 1)
    )
    system = (
        "You are a SECOND classification reviewer. A first pass produced a ranked list of "
        "candidate commodity codes. Audit it against the section/chapter EXCLUSION notes and "
        "the trader's facts. If a higher-ranked code falls under an excluded branch, DEMOTE it "
        "below codes that do not. You may ONLY reorder the existing codes - never add or remove "
        "any. Call resubmit_ranking with the full reordered list."
    )
    user = "\n\n".join(p for p in [
        f"## Trader query\n{raw_query}",
        f"## Prior Q&A\n{qa_text}",
        f"## First-pass ranking (reorder these only)\n{sheet}",
        excl_block or "## Exclusion notes\n(none in scope)",
    ] if p)
    tool = {
        "type": "function",
        "function": {
            "name": "resubmit_ranking",
            "description": "Resubmit the SAME codes, reordered if exclusions warrant it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ranked_codes": {
                        "type": "array",
                        "items": {"type": "string", "enum": codes},
                        "description": "All of the original codes, reordered (most likely first).",
                    },
                    "overturned": {"type": "boolean"},
                },
                "required": ["ranked_codes"],
            },
        },
    }
    model = os.environ.get("CLASSIFY_LLM_MODEL", "gpt-5.5")
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "tools": [tool],
        "tool_choice": "required",
    }
    _apply_llm_tuning(kwargs, model)
    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(**kwargs)
        calls = resp.choices[0].message.tool_calls or []
        if not calls:
            return answers, debug
        args = json.loads(calls[0].function.arguments or "{}")
    except Exception as e:
        debug["api_error"] = repr(e)
        return answers, debug

    ranked = [c for c in (args.get("ranked_codes") or []) if c in cand_map]
    # Re-attach any codes the model dropped, preserving original order, so the
    # set is never shrunk by verification.
    for a in answers:
        if a["commodity_code"] not in ranked:
            ranked.append(a["commodity_code"])
    by_code = {a["commodity_code"]: a for a in answers}
    reordered = [by_code[c] for c in ranked if c in by_code]
    debug["applied"] = True
    debug["overturned"] = bool(args.get("overturned")) or (ranked[: len(codes)] != codes)
    return reordered, debug


# --- Helpers -----------------------------------------------------------

def _enrich_candidates(candidates: list[dict]) -> list[dict]:
    """Tag each candidate with whether we have a fact sheet for it (slice).

    Preserves session_fact_* trace fields if the candidate came through the
    session-fact re-ranker, so the UI/eval can show why each code moved.
    """
    facets_slice = load_local_commodities()
    display_descs: dict[str, str] = {}
    generic_codes = [
        c["commodity_code"]
        for c in candidates
        if _is_generic_description(str(c.get("description") or ""))
    ]
    if generic_codes:
        try:
            display_descs = db_display_descriptions_for_codes(generic_codes)
        except Exception as exc:
            print(f"[classify] display description lookup failed: {exc!r}")
    out = []
    for c in candidates:
        code = c["commodity_code"]
        in_slice = code in facets_slice or _to_dotted(code) in facets_slice
        description = str(c.get("description") or "")
        display_description = display_descs.get(code) if _is_generic_description(description) else None
        enriched = {
            "commodity_code": code,
            "code_dotted": _to_dotted(code),
            "description": display_description or description,
            "score": c["score"],
            "sources": c.get("sources", []),
            "in_slice": in_slice,
        }
        # Preserve session-fact trace if present
        for k in ("session_fact_matches", "session_fact_contradictions", "session_score_delta"):
            if k in c:
                enriched[k] = c[k]
        out.append(enriched)
    return out


def _is_generic_description(description: str) -> bool:
    cleaned = re.sub(r"\s+", " ", description or "").strip().lower()
    return cleaned in {"", "other", "others", "other:"} or cleaned.startswith("other ")


def _is_non_classification_facet(key: str) -> bool:
    lowered = key.lower()
    skip_markers = (
        "origin", "country", "destination", "geograph", "import_date", "date",
        "duty", "vat", "preference", "quota", "measure", "certificate",
        "document", "licence", "license", "relief", "suspension",
        "common_term", "search_reference", "alias",
        "exclude", "excluded", "excludes",
    )
    return any(marker in lowered for marker in skip_markers)


def _normalised_use_scopes(row: dict) -> set[str]:
    raw = row.get("use_scopes")
    if not raw:
        return set()
    if isinstance(raw, str):
        raw = [raw]
    try:
        return {str(s).strip().lower() for s in raw if str(s).strip()}
    except TypeError:
        return set()


def _facet_allows_scope(facet: dict, scope: str) -> bool:
    scopes = _normalised_use_scopes(facet)
    if scopes:
        return scope.lower() in scopes
    key = str(facet.get("facet_key") or "")
    return not _is_non_classification_facet(key)


def _edge_allows_scope(edge: dict, scope: str) -> bool:
    scopes = _normalised_use_scopes(edge)
    if scopes:
        return scope.lower() in scopes
    edge_type = str(edge.get("type") or "").lower()
    if edge_type in {"duty_treatment", "footnote"}:
        return False
    return True


def _facet_question_priority(key: str) -> int:
    """Lower means earlier question under a GIR-style classification order."""
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


def _facet_question_priority_label(priority: int) -> str:
    labels = {
        0: "GIR 1 legal note / heading scope",
        1: "GIR 1 product identity or use",
        2: "GIR 3(b) material / essential character",
        3: "GIR 1/6 form or presentation",
        4: "GIR 6 composition threshold",
        5: "GIR 6 packaging / quantity threshold",
        6: "fallback discriminator",
    }
    return labels.get(priority, "fallback discriminator")


def _smooth_facet_question(
    facet_key: str,
    options: list[str],
    label: str | None = None,
    cfg: dict | None = None,
    api_key: str | None = None,
    allow_provider: bool = False,
) -> tuple[str, list[str]]:
    clean_key = (label or facet_key or "description").replace("_", " ").strip()
    lowered = facet_key.lower()
    if any(marker in lowered for marker in ("material", "composition", "ingredient", "component", "substance")):
        question = "What is the goods mainly made from?"
    elif any(marker in lowered for marker in ("function", "use", "purpose")):
        question = "What is the goods mainly used for?"
    elif any(marker in lowered for marker in ("form", "state", "processing", "presentation")):
        question = "What form are the goods in?"
    elif any(marker in lowered for marker in ("content", "protein", "fat", "sugar", "alcohol", "abv", "concentration")):
        question = "Which composition detail best matches the goods?"
    elif any(marker in lowered for marker in ("package", "packing", "net", "weight", "volume", "size", "container")):
        question = "How are the goods presented or packed?"
    else:
        question = f"Which {clean_key.lower()} best describes the goods?"

    display_options = [_smooth_option_value(option) for option in options]
    llm_enabled = bool((cfg or {}).get("use_llm_question_wording"))
    if llm_enabled and (api_key or _llm_question_wording_enabled(cfg or {})):
        question = _llm_rewrite_question(
            question=question,
            options=display_options,
            intent=(
                "Ask one trader-answerable classification question. Follow GIR order: "
                "heading/legal identity first, then material or essential character, then "
                "form/presentation, then subheading thresholds."
            ),
            model=_selected_question_model(cfg or {}),
            api_key_override=api_key,
            allow_provider_override=allow_provider,
        )
    return question, display_options


def _smooth_option_value(value: str) -> str:
    text = _clean_product_phrase(str(value or "").replace("_", " "))
    replacements = {
        "Cocoa preparation": "Chocolate or cocoa preparation",
        "Other": "Different product type",
        "N e s": "Not elsewhere specified",
    }
    return replacements.get(text, text) or "Different product type"


def _llm_question_wording_enabled(cfg: dict) -> bool:
    if not cfg.get("use_llm_question_wording"):
        return False
    if os.environ.get("JOURNEY_CLASSIFY_MODE", "").strip().lower() in {"deterministic", "offline"}:
        return False
    return openai_allowed()


def _llm_rewrite_question(
    question: str,
    options: list[str],
    intent: str,
    model: str | None = None,
    api_key_override: str | None = None,
    allow_provider_override: bool = False,
) -> str:
    api_key = api_key_override or os.environ.get("OPENAI_API_KEY")
    if not ((allow_provider_override and api_key) or (openai_allowed() and api_key)):
        return question
    try:
        from openai import OpenAI
    except Exception:
        return question
    system = (
        "Rewrite a customs classification question for a real trader. "
        "Keep it short, plain-English, and answerable from commercial/product knowledge. "
        "Do not mention commodity codes, tariff headings, GIRs, legal rules, country of origin, "
        "duty, VAT, certificates, or import dates. Do not add, remove, or imply answer options. "
        "Return JSON only: {\"question\": \"...\"}."
    )
    user = json.dumps({
        "intent": intent,
        "draft_question": question,
        "fixed_options": options[:8],
    })
    chosen_model = model or _selected_candidate_model()
    kwargs: dict = {
        "model": chosen_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    _apply_llm_tuning(kwargs, chosen_model)
    try:
        resp = OpenAI(api_key=api_key).chat.completions.create(**kwargs)
        parsed = json.loads(resp.choices[0].message.content or "{}")
        rewritten = _clean_option_text(str(parsed.get("question") or ""))
        if rewritten and 12 <= len(rewritten) <= 140 and "commodity code" not in rewritten.lower():
            return rewritten if rewritten.endswith("?") else f"{rewritten}?"
    except Exception as exc:
        print(f"[question wording LLM] {type(exc).__name__}: {exc}")
    return question


def _llm_candidate_question(
    candidates: list[dict],
    hydrated: list[dict] | dict | None,
    fallback_question: str,
    fallback_options: list[str],
    qa_history: list[dict] | None = None,
    limit: int = 6,
    model: str | None = None,
    api_key_override: str | None = None,
    allow_provider_override: bool = False,
) -> dict | None:
    api_key = api_key_override or os.environ.get("OPENAI_API_KEY")
    if not ((allow_provider_override and api_key) or (openai_allowed() and api_key)):
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None

    hydrated_by_code = _hydration_by_code(hydrated)
    context_rows = []
    for candidate in _enrich_candidates(candidates[:limit]):
        code = str(candidate.get("commodity_code") or "")
        hydration = hydrated_by_code.get(code) or {}
        evidence = []
        for item in hydration.get("evidence") or []:
            if not isinstance(item, dict) or item.get("kind") != "facet":
                continue
            if not _facet_allows_scope(item, "qa"):
                continue
            title = _clean_option_text(str(item.get("title") or ""))
            body = _clean_option_text(str(item.get("body") or ""))
            if title:
                evidence.append(_truncate_words(f"{title}: {body}" if body else title, 80))
            if len(evidence) >= 5:
                break
        context_rows.append({
            "description": _truncate_words(
                _strip_tariff_qualifiers(str(candidate.get("description") or "")),
                90,
            ),
            "facet_evidence": evidence,
            "fallback_option": _candidate_plain_option(candidate, hydration),
        })

    system = (
        "Create one plain-English customs classification question and answer options for a trader. "
        "Use only the supplied candidate descriptions and facet evidence. Do not mention commodity codes, "
        "tariff headings, GIRs, legal rules, duty, VAT, origin, certificates, or import dates. "
        "The options must describe product facts a trader can answer. Return JSON only: "
        "{\"question\":\"...\",\"options\":[\"...\"]}."
    )
    user = json.dumps({
        "intent": "Help a trader choose the closest product bucket from an already-retrieved hydrated shortlist.",
        "prior_qa": qa_history or [],
        "fallback_question": fallback_question,
        "fallback_options": fallback_options[:8],
        "hydrated_candidates": context_rows,
    })
    chosen_model = model or _selected_candidate_model()
    kwargs: dict = {
        "model": chosen_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
    }
    _apply_llm_tuning(kwargs, chosen_model)
    try:
        resp = OpenAI(api_key=api_key).chat.completions.create(**kwargs)
        parsed = json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:
        print(f"[candidate question LLM] {type(exc).__name__}: {exc}")
        return None

    question = _clean_option_text(str(parsed.get("question") or ""))
    if not question or len(question) < 12 or len(question) > 160:
        return None
    if "commodity code" in question.lower() or "tariff" in question.lower():
        return None
    if not question.endswith("?"):
        question = f"{question}?"

    options: list[str] = []
    for raw in parsed.get("options") or []:
        option = _clean_option_text(str(raw or ""))
        lowered = option.lower()
        if not option or len(option) > 120:
            continue
        if "commodity code" in lowered or "tariff" in lowered:
            continue
        if option not in options:
            options.append(option)
        if len(options) >= 7:
            break
    if len(options) < 2:
        return None
    none_option = "None of these are close; keep the broader shortlist"
    if none_option not in options:
        options.append(none_option)
    return {"question": question, "options": options}


_QUESTION_MODE_ALIASES = {
    "facets": "facet_rules",
    "facet": "facet_rules",
    "deterministic": "facet_rules",
    "facet_rules": "facet_rules",
    "facet_rules_llm": "facet_rules_llm_wording",
    "facet_rules_llm_wording": "facet_rules_llm_wording",
    "llm_wording": "facet_rules_llm_wording",
    "llm": "llm_generated",
    "llm_generated": "llm_generated",
}


def normalize_candidate_question_mode(mode: str | None) -> str:
    key = str(mode or "facet_rules").strip().lower().replace("-", "_")
    return _QUESTION_MODE_ALIASES.get(key, "facet_rules")


def hydrated_candidate_qa(
    candidates: list[dict],
    hydrated: list[dict] | dict | None = None,
    qa_history: list[dict] | None = None,
    question_mode: str | None = None,
    limit: int = 6,
    cfg: dict | None = None,
    model: str | None = None,
    api_key: str | None = None,
    allow_provider: bool = False,
) -> dict:
    requested_mode = normalize_candidate_question_mode(question_mode)
    cfg = {**DEFAULT_CLASSIFY_CONFIG, **(cfg or {})}
    history = [h for h in (qa_history or []) if isinstance(h, dict)]
    enriched = _enrich_candidates(candidates)
    facet_lookup = db_facets_for_codes([_candidate_code(c) for c in enriched])
    in_scope, out_of_scope, filter_trace = _apply_candidate_qa_history(
        enriched,
        facet_lookup,
        history,
        hydrated=hydrated,
    )

    scoped = in_scope or enriched
    qa_state = {
        "qa_history": history,
        "round": len(history) + 1,
        "in_scope_count": len(scoped),
        "out_of_scope_count": len(out_of_scope),
        "in_scope_codes": [_candidate_code(c) for c in scoped],
        "out_of_scope_codes": [_candidate_code(c) for c in out_of_scope],
        "filter_trace": filter_trace,
    }

    provider_allowed = bool(allow_provider and api_key)
    if requested_mode == "llm_generated" and provider_allowed:
        fallback_question, fallback_options = _candidate_fallback_question(
            scoped,
            hydrated=hydrated,
            limit=limit,
        )
        llm_question = _llm_candidate_question(
            candidates=scoped,
            hydrated=hydrated,
            fallback_question=fallback_question,
            fallback_options=fallback_options,
            qa_history=history,
            limit=limit,
            model=model,
            api_key_override=api_key,
            allow_provider_override=allow_provider,
        )
        if llm_question:
            return {
                "question_hint": {
                    "question": llm_question["question"],
                    "options": llm_question["options"],
                    "source": "llm_generated_hydrated_shortlist" if hydrated else "llm_generated_retrieval_shortlist",
                    "mode": "llm_generated",
                    "requested_mode": requested_mode,
                    "model": model or _selected_candidate_model(),
                    "provider_used": True,
                },
                "qa_state": qa_state,
            }

    use_llm_wording = requested_mode == "facet_rules_llm_wording" and provider_allowed
    facet_hint = _facet_rules_question_hint(
        scoped,
        facet_lookup,
        cfg={**cfg, "use_llm_question_wording": use_llm_wording},
        api_key=api_key if use_llm_wording else None,
        allow_provider=allow_provider if use_llm_wording else False,
    )
    if facet_hint:
        facet_hint.update({
            "mode": "facet_rules_llm_wording" if use_llm_wording else "facet_rules",
            "requested_mode": requested_mode,
            "provider_used": bool(use_llm_wording),
            "model": (model or _selected_question_model(cfg)) if use_llm_wording else None,
            "source": "facet_rules_llm_wording" if use_llm_wording else "facet_rules",
        })
        return {"question_hint": facet_hint, "qa_state": qa_state}

    fallback_question = smooth_candidate_question(
        scoped,
        hydrated=hydrated,
        limit=limit,
        use_llm=False,
        question_mode="facet_rules",
    )
    fallback_question.update({
        "requested_mode": requested_mode,
        "fallback_reason": "no discriminating facet met coverage/distinct-value thresholds",
    })
    return {"question_hint": fallback_question, "qa_state": qa_state}


def _candidate_fallback_question(
    candidates: list[dict],
    hydrated: list[dict] | dict | None = None,
    limit: int = 6,
) -> tuple[str, list[str]]:
    question = "In plain language, which bucket is closest to your goods?"
    return question, _candidate_description_options(candidates, limit=limit, hydrated=hydrated)


def _candidate_code(candidate: dict) -> str:
    return str(candidate.get("commodity_code") or candidate.get("code") or "")


def _facets_for_candidate(code: str, facet_lookup: dict[str, list[dict]]) -> list[dict]:
    return facet_lookup.get(code) or facet_lookup.get(_to_dotted(code)) or []


def _facet_rules_question_hint(
    candidates: list[dict],
    facet_lookup: dict[str, list[dict]],
    cfg: dict,
    api_key: str | None = None,
    allow_provider: bool = False,
) -> dict | None:
    scoped_lookup = {
        _candidate_code(candidate): _facets_for_candidate(_candidate_code(candidate), facet_lookup)
        for candidate in candidates
        if _candidate_code(candidate)
    }
    chosen_key, chosen_options, entropy, debug = _pick_facet_by_entropy(scoped_lookup)
    if not chosen_key or not chosen_options:
        return None

    label = chosen_key.replace("_", " ")
    try:
        defs = {d["key"]: d for d in db_facet_defs([chosen_key])}
        label = defs.get(chosen_key, {}).get("label") or label
    except Exception:
        pass

    question_text, display_options = _smooth_facet_question(
        chosen_key,
        chosen_options,
        label,
        cfg=cfg,
        api_key=api_key,
        allow_provider=allow_provider,
    )
    options_meta = _facet_option_meta(chosen_key, chosen_options, scoped_lookup)
    return {
        "question": question_text,
        "options": display_options[:6],
        "facet_key": chosen_key,
        "facet_label": label,
        "entropy": entropy,
        "options_meta": options_meta[:6],
        "debug": debug,
    }


def _facet_option_meta(
    facet_key: str,
    options: list[str],
    facet_lookup: dict[str, list[dict]],
) -> list[dict]:
    meta = []
    for option in options:
        label = _smooth_option_value(option)
        codes = []
        for code, facets in facet_lookup.items():
            for facet in facets:
                if facet.get("facet_key") != facet_key:
                    continue
                value = str(facet.get("facet_value") or "").strip().lower()
                if value == option:
                    codes.append(code)
                    break
        meta.append({
            "value": option,
            "label": label,
            "candidate_count": len(codes),
            "codes": codes[:20],
        })
    return meta


def _apply_candidate_qa_history(
    candidates: list[dict],
    facet_lookup: dict[str, list[dict]],
    qa_history: list[dict],
    hydrated: list[dict] | dict | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    active = list(candidates)
    ruled_out_by_code: dict[str, dict] = {}
    hydrated_by_code = _hydration_by_code(hydrated)
    trace: list[dict] = []
    for turn in qa_history:
        answer = str(turn.get("answer") or "").strip()
        if not answer or "none of these" in answer.lower():
            trace.append({"answer": answer, "matched_count": len(active), "action": "kept_all"})
            continue
        facet_key = str(turn.get("facet_key") or "").strip() or None
        answer_value = str(turn.get("answer_value") or "").strip().lower() or None
        matched = [
            candidate
            for candidate in active
            if _candidate_matches_qa_answer(
                candidate,
                answer,
                facet_lookup,
                hydrated_by_code.get(_candidate_code(candidate)),
                facet_key=facet_key,
                answer_value=answer_value,
            )
        ]
        if not matched:
            trace.append({"answer": answer, "facet_key": facet_key, "matched_count": 0, "action": "kept_all_no_match"})
            continue
        matched_codes = {_candidate_code(c) for c in matched}
        newly_ruled_out = [c for c in active if _candidate_code(c) not in matched_codes]
        for candidate in newly_ruled_out:
            code = _candidate_code(candidate)
            ruled_out_by_code[code] = {
                **candidate,
                "reason": f"Answer did not match {facet_key or 'candidate evidence'}: {answer}",
            }
        active = matched
        trace.append({
            "answer": answer,
            "facet_key": facet_key,
            "matched_count": len(matched),
            "ruled_out_count": len(newly_ruled_out),
            "action": "filtered",
        })
    return active, list(ruled_out_by_code.values()), trace


def _candidate_matches_qa_answer(
    candidate: dict,
    answer: str,
    facet_lookup: dict[str, list[dict]],
    hydration: dict | None = None,
    facet_key: str | None = None,
    answer_value: str | None = None,
) -> bool:
    code = _candidate_code(candidate)
    facets = _facets_for_candidate(code, facet_lookup)
    for facet in facets:
        if facet_key and facet.get("facet_key") != facet_key:
            continue
        if not _facet_allows_scope(facet, "qa"):
            continue
        value = str(facet.get("facet_value") or "").strip().lower()
        if answer_value and value == answer_value:
            return True
        if _answer_matches_text(answer, value) or _answer_matches_text(answer, _smooth_option_value(value)):
            return True
    if _answer_matches_text(answer, _candidate_plain_option(candidate, hydration)):
        return True
    if _answer_matches_text(answer, str(candidate.get("description") or "")):
        return True
    return False


def _answer_matches_text(answer: str, text: str) -> bool:
    left = _normal_match(answer)
    right = _normal_match(text)
    if not left or not right:
        return False
    if left == right:
        return True
    return (len(left) >= 4 and left in right) or (len(right) >= 4 and right in left)


def smooth_candidate_question(
    candidates: list[dict],
    hydrated: list[dict] | dict | None = None,
    limit: int = 6,
    use_llm: bool = False,
    model: str | None = None,
    api_key: str | None = None,
    allow_provider: bool = False,
    question_mode: str | None = None,
) -> dict:
    """Return a trader-facing fallback question for an already retrieved set.

    This is intentionally deterministic. Hydration can improve labels by
    supplying ATAR/facet/legal evidence for the shortlisted codes, but it must
    not create new candidate codes or ask about duty-only context.
    """
    requested_mode = normalize_candidate_question_mode(question_mode)
    if use_llm and requested_mode == "facet_rules":
        requested_mode = "llm_generated"
    question = "In plain language, which bucket is closest to your goods?"
    options = _candidate_description_options(candidates, limit=limit, hydrated=hydrated)
    llm_used = bool(requested_mode == "llm_generated" and use_llm and ((allow_provider and api_key) or openai_allowed()))
    if llm_used:
        llm_question = _llm_candidate_question(
            candidates=candidates,
            hydrated=hydrated,
            fallback_question=question,
            fallback_options=options,
            limit=limit,
            model=model,
            api_key_override=api_key,
            allow_provider_override=allow_provider,
        )
        if llm_question:
            return {
                "question": llm_question["question"],
                "options": llm_question["options"],
                "source": "llm_generated_hydrated_shortlist" if hydrated else "llm_generated_retrieval_shortlist",
                "mode": "llm_generated",
                "requested_mode": requested_mode,
                "model": model or _selected_candidate_model(),
                "provider_used": True,
            }
    return {
        "question": question,
        "options": options,
        "source": "facet_hydrated_shortlist" if hydrated else "facet_retrieval_shortlist",
        "mode": "facet_rules",
        "requested_mode": requested_mode,
        "provider_used": False,
    }


def _candidate_description_options(
    candidates: list[dict],
    limit: int = 6,
    hydrated: list[dict] | dict | None = None,
) -> list[str]:
    enriched = _enrich_candidates(candidates[:limit])
    hydrated_by_code = _hydration_by_code(hydrated)
    options: list[str] = []
    for candidate in enriched:
        code = str(candidate.get("commodity_code") or "")
        label = _candidate_plain_option(candidate, hydrated_by_code.get(code))
        if label and label not in options:
            options.append(label)
    if "None of these are close; keep the broader shortlist" not in options:
        options.append("None of these are close; keep the broader shortlist")
    return options or ["None of these are close; keep the broader shortlist"]


def _candidate_plain_option(candidate: dict, hydration: dict | None = None) -> str:
    """Convert a tariff candidate into a trader-answerable bucket label.

    The fallback Q&A must not ask traders to choose between commodity codes or
    legal descriptions. We keep the mapping deterministic by deriving a short
    plain-language label from the candidate code plus contextual description.
    """
    code = str(candidate.get("commodity_code") or "")
    desc = _clean_option_text(str(candidate.get("description") or ""))
    lowered = desc.lower()

    if code.startswith("18069070") or ("cocoa" in lowered and "beverage" in lowered):
        return "Cocoa drink mix or hot chocolate powder"
    if code.startswith("1806"):
        return "Chocolate or other cocoa preparation"
    if code.startswith("180500"):
        return "Plain cocoa powder with no added sugar"
    if code.startswith("220299"):
        if "protein content >= 2.8" in lowered or "protein content of >= 2.8" in lowered:
            return "Soya, nut or cereal drink with at least 2.8% protein"
        if "protein content < 2.8" in lowered or "protein content of < 2.8" in lowered:
            return "Soya, nut or cereal drink with under 2.8% protein"
        return "Soya, nut, seed or cereal-based drink"
    if code.startswith("0404") or "whey and modified whey" in lowered:
        if "without added sugar" in lowered or "not containing added sugar" in lowered:
            return "Whey powder without added sugar or sweetener"
        if "with added sugar" in lowered or "containing added sugar" in lowered:
            return "Whey powder with added sugar or sweetener"
        return "Whey powder"
    if "pea protein" in lowered:
        return "Pea protein powder or concentrate"
    if "concentrated milk proteins" in lowered:
        return "Concentrated milk protein powder"
    if "protein concentrates and textured protein substances" in lowered:
        return "Protein concentrate or textured protein product"
    if code.startswith("3504"):
        return "Milk proteins, peptones or other protein substances"
    if code.startswith("2106"):
        return "Flavoured syrup or other prepared food product"
    if code.startswith("1901"):
        return "Dough, bakery mix or flour/starch/milk food preparation"
    if code.startswith("2204"):
        return "Wine or grape must"
    if code.startswith("2208"):
        return "Spirits or other distilled alcoholic drink"
    if code.startswith("640"):
        return "Footwear"
    if code.startswith("610") or code.startswith("611") or code.startswith("620") or code.startswith("621"):
        return "Clothing or textile garment"
    if code.startswith("8504"):
        return "Power supply, charger or electrical transformer"
    if code.startswith("8517"):
        return "Phone, router or communications equipment"
    if code.startswith("8471"):
        return "Computer, storage or data-processing equipment"
    if code.startswith("9401"):
        return "Seat, chair or seating furniture"
    if code.startswith("9403"):
        return "Furniture or furniture part"
    if code.startswith("3926"):
        return "Plastic article or plastic part"
    if code.startswith("1704"):
        return "Sugar confectionery or sweets"
    if code.startswith("2103"):
        return "Sauce, seasoning or condiment"
    if code.startswith("4016"):
        return "Rubber article or rubber part"
    if code.startswith("7326"):
        return "Iron or steel part or article"

    aliases = _aliases_from_description(desc)
    if aliases:
        return _truncate_words(" / ".join(aliases[:2]), 90)
    hydrated_label = _plain_label_from_hydration(hydration)
    if hydrated_label:
        return _truncate_words(hydrated_label, 90)
    return _truncate_words(_strip_tariff_qualifiers(desc), 110) or "A different product type"


def _hydration_by_code(hydrated: list[dict] | dict | None) -> dict[str, dict]:
    if not hydrated:
        return {}
    if isinstance(hydrated, dict):
        return {str(k): v for k, v in hydrated.items() if isinstance(v, dict)}
    out: dict[str, dict] = {}
    for item in hydrated:
        payload = item.get("hydration") if isinstance(item, dict) else None
        if not isinstance(payload, dict):
            continue
        code = str(payload.get("commodity_code") or "")
        if code:
            out[code] = payload
    return out


def _plain_label_from_hydration(hydration: dict | None) -> str:
    if not hydration:
        return ""
    evidence = hydration.get("evidence") or []
    facet_priority = {
        "product type", "product_type", "common name", "common_name",
        "form", "material", "main material", "composition", "use", "function",
    }
    for item in evidence:
        if not isinstance(item, dict) or item.get("kind") != "facet":
            continue
        if not _facet_allows_scope(item, "qa"):
            continue
        title = _clean_option_text(str(item.get("title") or ""))
        if ":" not in title:
            continue
        key, value = [part.strip() for part in title.split(":", 1)]
        if key.lower() in facet_priority and value:
            return _clean_product_phrase(value)
    for item in evidence:
        if not isinstance(item, dict) or item.get("kind") != "atar":
            continue
        body = str(item.get("body") or "")
        match = re.search(r"(?:^|\n)\s*Product:\s*(.+?)(?:\n|$)", body, flags=re.I | re.S)
        if match:
            label = _clean_product_phrase(match.group(1))
            if label:
                return label
    commodity = hydration.get("commodity") or {}
    if isinstance(commodity, dict):
        label = _clean_product_phrase(str(commodity.get("description") or ""))
        if label and not _is_generic_description(label):
            return label
    return ""


def _clean_product_phrase(text: str) -> str:
    text = _clean_option_text(text)
    text = re.sub(r"^(?:the\s+)?product\s+(?:is|are)\s+", "", text, flags=re.I)
    text = re.sub(r"^(?:this|these)\s+(?:is|are)\s+", "", text, flags=re.I)
    text = re.sub(r"^(?:goods?|articles?)\s+(?:is|are)\s+", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" .:-")
    if text:
        text = text[0].upper() + text[1:]
    return text


def _clean_option_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" -")
    text = re.sub(r"^\d{10}\s*-\s*", "", text)
    return text


def _aliases_from_description(text: str) -> list[str]:
    marker = "also known as:"
    lower = text.lower()
    if marker not in lower:
        return []
    alias_text = text[lower.index(marker) + len(marker):]
    aliases = []
    for alias in re.split(r",|;|/", alias_text):
        cleaned = _strip_tariff_qualifiers(alias)
        if cleaned and len(cleaned) >= 3:
            aliases.append(cleaned)
    return aliases


def _strip_tariff_qualifiers(text: str) -> str:
    text = re.sub(r"\([^)]*(?:excl|incl|not elsewhere|n\.e\.s|<=|>=)[^)]*\)", "", text, flags=re.I)
    text = re.sub(r"\b(excl|incl)\.?\b.*$", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" .,-")
    return text


def _truncate_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].strip()
    return f"{cut}..." if cut else text[:limit]


def _normal_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _to_dotted(flat: str) -> str:
    digits = re.sub(r"\D", "", flat or "")
    if len(digits) < 10:
        return flat
    return f"{digits[0:4]}.{digits[4:6]}.{digits[6:8]}{digits[8:10] if digits[8:10] != '00' else ''}".rstrip(".")


def _facets_for(code: Optional[str]) -> Optional[dict]:
    if not code:
        return None
    facets_slice = load_local_commodities()
    item = facets_slice.get(code) or facets_slice.get(_to_dotted(code))
    if not item:
        return None
    return {
        "code": item.get("code"),
        "facets": item.get("facets", {}),
        "common_terms": item.get("common_terms", []),
        "self_text": item.get("self_text", ""),
        "kg_edges": item.get("kg_edges", []),
    }


def _kg_notes_for(code: Optional[str]) -> list[dict]:
    if not code:
        return []
    facets_slice = load_local_commodities()
    item = facets_slice.get(code) or facets_slice.get(_to_dotted(code))
    if not item:
        return []
    edge_ids = set(item.get("kg_edges", []))
    return [e for e in load_kg_edges() if e["id"] in edge_ids]


def _kg_notes_for_candidates(candidates: list[dict]) -> list[dict]:
    facets_slice = load_local_commodities()
    edge_ids: set[str] = set()
    for c in candidates:
        item = facets_slice.get(c["commodity_code"]) or facets_slice.get(c.get("code_dotted", ""))
        if item:
            edge_ids.update(item.get("kg_edges", []))
    return [e for e in load_kg_edges() if e["id"] in edge_ids]
