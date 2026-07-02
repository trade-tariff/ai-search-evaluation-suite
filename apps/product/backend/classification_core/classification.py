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
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

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
    "baseline", "rule_reasoning", "exclusion_aware", "gir_citation", "self_verify",
    "rank_all", "top_k_pressure", "facet_soft_score",
}


def _apply_llm_tuning(kwargs: dict, model: str) -> dict:
    """Mutate+return an OpenAI chat.completions kwargs dict with the model-aware
    tuning every call site needs:
      - per-request timeout (always)
      - reasoning_effort='minimal' for gpt-5*/o* reasoning models (latency/cost)
      - temperature=0.0 for non-reasoning models (determinism)
    Reasoning models reject `temperature`, so it is only set off the gpt-5/o path.
    """
    kwargs.setdefault("timeout", LLM_TIMEOUT_S)
    if model.startswith("gpt-5") or model.startswith("o"):
        kwargs.setdefault("reasoning_effort", os.environ.get("CLASSIFY_REASONING_EFFORT", "minimal"))
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
    "\n\n--- RANKING DISCIPLINE (rank_all mode) ---\n"
    "Hard exclusions remain conservative, but ranking is mandatory. Even when no candidate is "
    "definitively excluded, return survivors in a deliberately ranked likelihood order using the "
    "query, prior Q&A, retrieval scores, structured facts, and KG rules. Do not preserve the input "
    "or retrieval order unless you have actively judged it to be the likelihood order. A response "
    "that keeps all candidates in original order is invalid. Use Strong for a small top group, Good "
    "for close alternatives, and Possible for fallback survivors."
)

_TOP_K_PRESSURE_BLOCK = (
    "\n\n--- TOP-K PRESSURE (top_k_pressure mode) ---\n"
    "After applying any definitive exclusions, concentrate the ranked survivor list: identify the "
    "most plausible top 20 and put the strongest top 5 first. You may keep additional candidates "
    "as Possible, but the first 20 survivors must be actively reranked. Ask another question only "
    "if it is more useful than committing a ranked survivor list."
)

_FACET_SOFT_SCORE_BLOCK = (
    "\n\n--- FACET SOFT SCORING (facet_soft_score mode) ---\n"
    "Use structured facts/facets as soft scoring evidence: material, form, function, use, exclusions, "
    "and identity labels should move candidates up or down. Do not rule out solely because a facet is "
    "missing or normalized differently. Hard-rule-out only on explicit contradiction, binding KG rule, "
    "or an answer that cleanly maps to an incompatible fact. The final survivor order must reflect the "
    "combined facet/fact/KG evidence, not the original retrieval order."
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
    # exclusion_aware | gir_citation | self_verify | rank_all |
    # top_k_pressure | facet_soft_score.
    "prompt_mode": "baseline",
    "use_query_expansion": False,   # AI-441 - pre-LLM rewrite of query (default off; opt-in)
    "use_facets": True,             # STRUCTURED_FACTS section
    "use_session_facts": True,      # Extract facets from user input, re-rank + inject as HARD constraints
    # AI-440 style: server picks facet via entropy + builds options; LLM either
    # phrases the question or commits to a code. No invented options possible.
    "use_entropy_picker": True,
    "use_llm_question_wording": False,
    "question_wording_model": os.environ.get("QUESTION_WORDING_MODEL", os.environ.get("CLASSIFY_LLM_MODEL", "gpt-5-nano")),
    "retrieval": {                  # see local_db.retrieve_candidates
        # ---- Production defaults v2 (Exp 2+3 leg-ablation verdict) ----
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
    if os.environ.get("CLASSIFICATION_MODE", "").strip().lower() in {"deterministic", "offline"}:
        return {"mode": "deterministic", "reason": "Offline/deterministic classification mode is active."}
    if not openai_allowed():
        return {"mode": "deterministic", "reason": "Provider calls are not enabled."}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"mode": "deterministic", "reason": "OPENAI_API_KEY is not set."}
    return {"mode": "llm", "model": _selected_question_model(cfg)}


# The real OTT AI Guided Search context template - copied verbatim from
# ai-fan-out, then AUGMENTED with the AI-440 layers:
#   - STRUCTURED_FACTS: a facet matrix across the candidate set
#   - KG_EDGES: chapter/section notes, exclusions, ATAR rationales
# The LLM uses both to (a) pick a discriminating facet for the next question,
# (b) draw answer options from real facet values, (c) apply chapter rules.
CONTEXT_TEMPLATE = """You're an expert Harmonised System code classifier.

Look at the search input and any previously answered questions and decide whether more questions are needed to confidently assign a commodity code.

If answers are available, use them to help formulate your questions and answers - don't go beyond these search results in terms of the overall commodity hierarchy - even if you know the results are incorrect.

When STRUCTURED_FACTS are available for the candidate set, prefer to phrase the next question around a facet whose values DIFFER across candidates - and use the actual values present as options. Avoid asking about facets that all candidates share. When KG_EDGES are present, apply them as hard rules: if an edge says "this chapter does not cover X", do not propose X. If an edge specifies a classification order (e.g. "outer sole material first"), respect that order when asking.

## Response format

Respond in JSON format with one of the following:

### Confident answer

Rank the top 5 opensearch answers by confidence and provide the most likely answer if you are confident.

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


# ----- AI-440: symbolic info-gain + neural phrasing ---------------------

def _pick_facet_by_entropy(
    facet_lookup: dict[str, list[dict]],
    min_coverage: float = 0.25,
    min_distinct_values: int = 2,
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
            # Skip operational/commercial-context facets. They matter later for
            # handoff or rate, but they are hostile as classification Q&A.
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
    # legally/classificatorily decisive characteristic before rate context,
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
    structured_facts_text = (
        _structured_facts_section(codes) if cfg.get("use_facets") else "Disabled by config."
    )
    kg_edges_text = _kg_edges_section(codes, include=cfg.get("kg_include"))

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
    debug["prompt_mode"] = prompt_mode
    debug["prompt_chars"] = len(prompt)
    debug["sections"] = {
        "structured_facts_chars": len(structured_facts_text),
        "kg_edges_chars": len(kg_edges_text),
        "opensearch_chars": len(opensearch_text),
        "qa_chars": len(qa_text),
        "session_facts_count": len(session_facts or []),
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
        "(operational, rate, document, trade-geo, and Search References alias facts omitted):"
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


# --- Driver ------------------------------------------------------------

def classify_step(query: str, qa_history: list[dict], config: Optional[dict] = None) -> dict:
    cfg = {**DEFAULT_CLASSIFY_CONFIG, **(config or {})}
    retrieval_cfg = {**DEFAULT_CLASSIFY_CONFIG["retrieval"], **(cfg.get("retrieval") or {})}
    """Run one turn of the AI Guided Search loop.

    Returns a dict with:
      - candidates: list of {commodity_code, description, score, sources}
      - llm_response: parsed LLM JSON (or None)
      - mode: "answers" | "questions" | "error" | "no_candidates"
      - answers: parsed answers list (when mode==answers)
      - question: the single question we surface (when mode==questions)
      - kg_notes: any KG edges relevant to the in-slice candidates
      - facet_enrichment: facets dict for the top in-slice candidate (if any)
    """
    # Query expansion (AI-441): runs BEFORE retrieval so the rewritten query
    # feeds both legs of the system. Previously expansion only fed the LLM
    # prompt - retrieval still used the raw vague query. That was a bug.
    # Expand only on the first turn (qa_history empty); later turns already
    # have refining context from Q&A so re-expansion would muddy intent.
    expanded_query = query
    if cfg.get("use_query_expansion") and _use_llm_candidate_selection(cfg) and not qa_history:
        ex = _llm_expand_query(query)
        if ex and ex.strip() and ex.strip() != query.strip():
            expanded_query = ex.strip()

    # Session-time fact extraction: pull structured facts from the user's
    # input + qa_history so they can be used as a re-ranker on the candidate
    # list AND as HARD CONSTRAINTS in the classification prompt. Closes the
    # "user volunteers info we don't have facets for" gap.
    session_facts: list[dict] = []
    if cfg.get("use_session_facts"):
        try:
            from . import session_facts as sf_mod
            session_facts = sf_mod.extract_session_facts(query, qa_history)
        except Exception as exc:
            print(f"[classify] session_facts extraction failed: {exc!r}")

    candidates = db_retrieve(
        expanded_query,
        limit=int(retrieval_cfg.get("limit", 40)),
        use_curated=retrieval_cfg.get("use_curated", False),
        use_vector=retrieval_cfg.get("use_vector", False),
        use_facts=retrieval_cfg.get("use_facts_leg", True),
        use_kg_context=retrieval_cfg.get("use_kg_context_leg", True),
        use_facts_vec=retrieval_cfg.get("use_facts_vec_leg", True),
        use_kg_vec=retrieval_cfg.get("use_kg_vec_leg", True),
        facts_cap=float(retrieval_cfg.get("facts_cap", 0.5)),
        kg_cap=float(retrieval_cfg.get("kg_cap", 0.5)),
        facts_vec_cap=float(retrieval_cfg.get("facts_vec_cap", 0.9)),
        kg_vec_cap=float(retrieval_cfg.get("kg_vec_cap", 0.9)),
        rrf_k=int(retrieval_cfg.get("rrf_k", 60)),
        # LOO exclusions for honest eval - skip facts/edges derived from the
        # ATAR we're trying to classify (passed through from Exp 6).
        exclude_fact_sources=retrieval_cfg.get("exclude_fact_sources"),
        exclude_edge_ids=retrieval_cfg.get("exclude_edge_ids"),
        use_vec_adapter=retrieval_cfg.get("use_vec_adapter", False),
    )
    if not candidates:
        return {
            "candidates": [],
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

    # AI-440 path: server picks facet by entropy, LLM picks via function-calling.
    # Falls through to legacy LLM-only path if disabled or no good facet exists.
    if not _use_llm_candidate_selection(cfg) or os.environ.get("CLASSIFICATION_MODE", "").strip().lower() in {"deterministic", "offline"}:
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
        chosen_key, chosen_options, entropy, ep_debug = _pick_facet_by_entropy(facet_lookup)
        structured_facts_text = (
            _structured_facts_section(cand_codes_for_facets)
            if cfg.get("use_facets") else "Disabled by config."
        )
        kg_edges_text = _kg_edges_section(cand_codes_for_facets, include=cfg.get("kg_include"))
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
        parsed, debug = _llm_classify(
            query, candidates, qa_history,
            config=config, expanded_query=expanded_query,
            session_facts=session_facts,
        )
    parsed = parsed or {}
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
        top = enriched_answers[0] if enriched_answers else None
        top_code = top["commodity_code"] if top else None
        return {
            "candidates": candidates_out,
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
        return {
            "candidates": candidates_out,
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
        return {
            "candidates": candidates_out,
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
    return {
        "candidates": candidates_out,
        "llm_response": None,
        "mode": "answers",
        "answers": [
            {
                "commodity_code": c["commodity_code"],
                "confidence": "From retrieval",
                "description": c["description"],
            }
            for c in candidates_out[:5]
        ],
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
        ex = _llm_expand_query(query)
        if ex and ex.strip() and ex.strip() != query.strip():
            expanded_query = ex.strip()

    candidates = db_retrieve(
        expanded_query,
        limit=candidate_limit,
        use_curated=retrieval_cfg.get("use_curated", False),
        use_vector=retrieval_cfg.get("use_vector", False),
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

    if not qa_history and cfg.get("qa_mode", "ask_first") != "answers_only":
        first_question = _deterministic_eliminate_turn(
            query=query,
            qa_history=qa_history,
            fixed_candidates=fixed_candidates,
            candidates_out=candidates_out,
            cfg=cfg,
            session_facts=session_facts,
            force_question=True,
        )
        if first_question and first_question.get("mode") == "questions":
            return first_question

    if (
        not _use_llm_candidate_selection(cfg)
        or os.environ.get("CLASSIFICATION_MODE", "").strip().lower() in {"deterministic", "offline"}
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
    fallback_to_retrieval_order = False
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
        fallback_to_retrieval_order = True
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
            "prompt_mode": prompt_mode,
            "fallback_to_retrieval_order": fallback_to_retrieval_order,
            "raw_survivor_count": len(survivors_raw) if isinstance(survivors_raw, list) else None,
            "parsed_survivor_count": len(survivors),
            "raw_ruled_out_count": len(ruled_out) if isinstance(ruled_out, list) else None,
        },
        "question_wording": _question_wording_summary(cfg),
        "frozen_candidate_count": len(fixed_candidates),
        "survivor_count": len(survivors),
        "ruled_out_count": len(ruled_out),
        "session_facts": session_facts,
        "session_facts_count": len(session_facts),
        "debug": debug,
    }

    # Did the LLM ask for one more fact? Only honour it if options are present.
    q = parsed.get("question")
    if isinstance(q, dict) and q.get("question") and q.get("options"):
        return {
            "candidates": candidates_out,
            "llm_response": parsed,
            "mode": "questions",
            "answers": [],
            "question": {"question": q.get("question", ""), "options": list(q.get("options", []))},
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

    top_code = survivors[0]["commodity_code"] if survivors else None
    return {
        "candidates": candidates_out,
        "llm_response": parsed,
        "mode": "answers",
        "answers": survivors[:5],
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
        top = answers[0]["commodity_code"] if answers else None
        return {
            "candidates": filtered_out,
            "fixed_candidates": fixed_candidates,
            "llm_response": None,
            "mode": "answers",
            "answers": answers,
            "question": None,
            "kg_notes": _kg_notes_for(top) if top else [],
            "facet_enrichment": _facets_for(top) if top else None,
            "augmentation_summary": summary({"answer_matched_codes": sorted(matched_codes)}, len(filtered)),
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
            "llm_response": None,
            "mode": "questions",
            "answers": [],
            "question": {
                "question": question_text,
                "options": display_options[:6],
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
            "llm_response": None,
            "mode": "questions",
            "answers": [],
            "question": {
                "question": question_hint["question"],
                "options": options,
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
    top = answers[0]["commodity_code"] if answers else None
    return {
        "candidates": candidates_out,
        "fixed_candidates": fixed_candidates,
        "llm_response": None,
        "mode": "answers",
        "answers": answers,
        "question": None,
        "kg_notes": _kg_notes_for(top) if top else [],
        "facet_enrichment": _facets_for(top) if top else None,
        "augmentation_summary": summary({"reason": "no discriminating facet"}),
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

    model = model or _selected_candidate_model()
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "tools": [tool],
        "tool_choice": "required",
    }
    debug["model"] = model
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
        debug["raw_tool_arguments_preview"] = (tool_calls[0].function.arguments or "")[:4000]
        return None, debug
    debug["tool_args_keys"] = sorted(args.keys())
    debug["tool_survivor_count"] = len(args.get("survivors") or []) if isinstance(args.get("survivors"), list) else None
    debug["tool_ruled_out_count"] = len(args.get("ruled_out") or []) if isinstance(args.get("ruled_out"), list) else None
    debug["tool_question_returned"] = bool(isinstance(args.get("question"), dict) and args.get("question", {}).get("question"))
    debug["raw_tool_arguments_preview"] = json.dumps(args, default=str)[:8000]
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
        "rate", "vat", "preference", "quota", "measure", "certificate",
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
    if edge_type in {"operational_treatment", "footnote"}:
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
    if _llm_question_wording_enabled(cfg or {}):
        question = _llm_rewrite_question(
            question=question,
            options=display_options,
            intent=(
                "Ask one trader-answerable classification question. Follow GIR order: "
                "heading/legal identity first, then material or essential character, then "
                "form/presentation, then subheading thresholds."
            ),
            model=_selected_question_model(cfg or {}),
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
    if os.environ.get("CLASSIFICATION_MODE", "").strip().lower() in {"deterministic", "offline"}:
        return False
    return openai_allowed()


def _llm_rewrite_question(question: str, options: list[str], intent: str, model: str | None = None) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_allowed() or not api_key:
        return question
    try:
        from openai import OpenAI
    except Exception:
        return question
    system = (
        "Rewrite a customs classification question for a real trader. "
        "Keep it short, plain-English, and answerable from commercial/product knowledge. "
        "Do not mention commodity codes, tariff headings, GIRs, legal rules, country of origin, "
        "rate, VAT, certificates, or import dates. Do not add, remove, or imply answer options. "
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


def smooth_candidate_question(
    candidates: list[dict],
    hydrated: list[dict] | dict | None = None,
    limit: int = 6,
    use_llm: bool = False,
    model: str | None = None,
) -> dict:
    """Return a trader-facing fallback question for an already retrieved set.

    This is intentionally deterministic. Hydration can improve labels by
    supplying ATAR/facet/legal evidence for the shortlisted codes, but it must
    not create new candidate codes or ask about rate-only context.
    """
    question = "In plain language, which bucket is closest to your goods?"
    options = _candidate_description_options(candidates, limit=limit, hydrated=hydrated)
    llm_used = bool(use_llm and openai_allowed())
    if llm_used:
        question = _llm_rewrite_question(
            question=question,
            options=options,
            intent="Help a trader choose the closest product bucket from an already-retrieved shortlist.",
            model=model,
        )
    return {
        "question": question,
        "options": options,
        "source": ("llm_hydrated_shortlist" if hydrated else "llm_retrieval_shortlist") if llm_used else ("hydrated_shortlist" if hydrated else "retrieval_shortlist"),
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
