"""Query-side lever: rewrite a trader query into tariff vocabulary BEFORE retrieval.

Mirrors production's ExpandSearchQueryService (AI-815, Done) and the "Triage" winner
from AI-836 (Ben Coombe): the single biggest improvement came from restructuring the
trader's query into classification-oriented tariff language before search, not from
reranking/pruning candidates. Graceful fallback to the original query on error, numeric
input, or no API key (same as production).

v1 = the query-expansion core (AI-815). Not yet the full triage (HS-chapter shortlist +
ambiguity-blocking clarifying question) - that's the next increment.
"""
from __future__ import annotations

import json
import os
import re

from . import local_db

EXPAND_MODEL = os.environ.get("TRIAGE_MODEL", "gpt-5-mini")

# Shared persistent cache of query rewrites. Many eval configs (and prod replays)
# recompute the SAME per-query LLM rewrite; this de-dups them across processes.
# Robust by design: any cache read/write/connection error falls back to computing
# the rewrite as if the cache did not exist (never crashes the caller).
_WS_RE = re.compile(r"\s+")


def _norm_query(query: str) -> str:
    """Normalise a query for cache keying: lower + strip + collapse whitespace."""
    return _WS_RE.sub(" ", query.strip().lower())


def _cache_get(key: str) -> str | None:
    try:
        with local_db._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT rewrite FROM kg.triage_cache WHERE query_norm = %s", (key,))
            row = cur.fetchone()
        return row["rewrite"] if row else None
    except Exception as exc:
        print(f"[triage] cache get failed: {exc!r}")
        return None


def _cache_put(key: str, rewrite: str) -> None:
    try:
        with local_db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kg.triage_cache (query_norm, rewrite) VALUES (%s, %s) "
                "ON CONFLICT (query_norm) DO NOTHING",
                (key, rewrite),
            )
            conn.commit()
    except Exception as exc:
        print(f"[triage] cache put failed: {exc!r}")


_SYS = """You rewrite a trader's plain-language product description into the formal
vocabulary of the UK / Harmonised System customs tariff, to improve commodity-code
retrieval. Output one enriched query (1-2 sentences) in tariff terms - material,
function, form, processing, composition, intended use - keeping the trader's specifics
and adding tariff-language synonyms. Do NOT guess a commodity code.
Output JSON only: {"expanded": "<rewritten query>"}"""

# Production's ExpandSearchQueryService prompt, verbatim from staging's
# uk.admin_configurations 'expand_query_context' (model gpt-4.1-mini-2025-04-14).
# Single context string with %{search_query} substituted; returns {expanded_query, reason}.
_STAGING_SYS = """You are an expert in trade tariff classification and search queries.

Your task is to rephrase and expand a given search query so it matches trade commodities more effectively.
The goal is to generate a query likely to match relevant tariff data, especially when the original query does not
use official terminology found in commodity descriptions and supporting classification text.

Provide only the rephrased and expanded search query as plain text, without extra formatting or explanation.

**Original search query:** %{search_query}

## Output format

Return the expanded search query in the following JSON format:

    {
      "expanded_query": "string",
      "reason": "string"
    }

The reason for the expansion should briefly explain why the changes were made to improve search effectiveness.

## Example

For the search query "laptop":

    {
      "expanded_query": "Portable automatic data-processing machines",
      "reason": "The term 'laptop' is a common colloquial term, but the official tariff classification uses more formal terminology."
    }"""


def expand_query(query: str, qa_history: list[dict] | None = None,
                 model: str | None = None, prompt_variant: str = "mine") -> str:
    """Return the tariff-vocabulary-enriched query, or the original on any fallback.

    model: LLM to use (defaults to TRIAGE_MODEL env / gpt-5-mini).
    prompt_variant: "mine" (eval's tariff-vocab prompt) or "staging" (production's
        ExpandSearchQueryService expand_query_context prompt). Lets us compare the
        production rewrite (gpt-4.1-mini + staging prompt) vs the eval's (gpt-5-mini + mine).
    """
    if not query or not query.strip():
        return query
    if query.strip().replace(" ", "").replace(".", "").isdigit():
        return query  # numeric / exact-code: skip expansion (matches production)
    mdl = model or EXPAND_MODEL
    variant = prompt_variant or "mine"
    # Cache key MUST include model + prompt variant or different rewrites collide on
    # query_norm. Keep the historical plain key for the default (gpt-5-mini + mine) so
    # the existing warm cache still hits. No caching when qa_history is present.
    if qa_history:
        cache_key = None
    elif mdl == "gpt-5-mini" and variant == "mine":
        cache_key = _norm_query(query)
    else:
        cache_key = f"{mdl}::{variant}::{_norm_query(query)}"
    if cache_key is not None:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return query
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=30.0, max_retries=2)
        ctx = query.strip()
        if qa_history:
            ctx += "\nClarifications: " + "; ".join(
                f"{h.get('question','')} -> {h.get('answer','')}" for h in qa_history)
        extra = {"reasoning_effort": os.environ.get("TRIAGE_REASONING_EFFORT", "low")} if mdl.startswith("gpt-5") else {}
        if variant == "staging":
            # Faithful to production: single context string, returns {"expanded_query": ...}.
            content = _STAGING_SYS.replace("%{search_query}", ctx)
            resp = client.chat.completions.create(
                model=mdl, response_format={"type": "json_object"},
                messages=[{"role": "user", "content": content}], **extra)
            exp = (json.loads(resp.choices[0].message.content or "{}").get("expanded_query") or "").strip()
        else:
            resp = client.chat.completions.create(
                model=mdl, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _SYS}, {"role": "user", "content": ctx}], **extra)
            exp = (json.loads(resp.choices[0].message.content or "{}").get("expanded") or "").strip()
        result = exp or query
        if cache_key is not None:
            _cache_put(cache_key, result)
        return result
    except Exception as exc:
        print(f"[triage] expand failed: {exc!r}")
        return query
