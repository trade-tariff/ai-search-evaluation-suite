"""Multi-query retrieval expansion.

For vague queries ("metal rails", "machine part"), a single retrieval call
anchors on the most-obvious semantic and misses Mode-B cross-domain golds.

This module rewrites a vague query into 3-5 specific reformulations covering
the plausible interpretations, runs retrieve_candidates for each, and unions
the results keeping the max score per code. Effective at expanding coverage
without changing the retrieval primitive itself.

Cost: 1 LLM call (gpt-5-mini) to expand + N retrieval calls (DB only after
embedding is cached). ~$0.001 per query.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from . import local_db


EXPANDER_MODEL = os.environ.get("MULTI_QUERY_MODEL", "gpt-5-mini")
DEFAULT_N_VARIANTS = int(os.environ.get("MULTI_QUERY_N", "4"))


_SYSTEM = """You expand vague UK customs classification queries into 3-5 specific
reformulations covering plausibly-different interpretations.

The goal: catch CROSS-DOMAIN ambiguity. A query like "metal rails" could mean
railway rails, decorative wall rails, or curtain rails - each in a different
HS chapter. Your expansions force the retrieval pipeline to consider each
domain.

Rules:
- 3-5 reformulations, each 3-10 words.
- Each reformulation should NARROW to a specific concept (function, material, domain).
- Prefer commodity-shop language over technical: "running shoes" not "athletic footwear".
- Do NOT add interpretations the original query rules out ("leather shoes" -> don't
  expand to "plastic shoes").
- ALWAYS include the original query as variant #1, verbatim, so we don't lose
  the trader's natural phrasing.

Output JSON only:
{"variants": ["<original>", "<variant 2>", "<variant 3>", ...]}
"""


def expand_query_to_variants(query: str, n: int = DEFAULT_N_VARIANTS) -> list[str]:
    """Returns [original, variant_2, ..., variant_n]. Always includes the
    original as variant #1 so we never lose recall on the verbatim path.

    Falls back to just [query] on any error.
    """
    if not query or not query.strip():
        return [query]
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return [query]
    try:
        from openai import OpenAI
    except Exception:
        return [query]
    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=EXPANDER_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Expand: {query.strip()}\nTarget: {n} variants total."},
            ],
            response_format={"type": "json_object"},
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return [query]
        data = json.loads(content)
        variants = data.get("variants") or []
        cleaned: list[str] = []
        for v in variants:
            s = str(v).strip().strip("\"'")
            if s and s not in cleaned:
                cleaned.append(s)
        if not cleaned:
            return [query]
        # Guarantee the original is first - the prompt asks for it, but enforce.
        if cleaned[0].lower() != query.strip().lower():
            cleaned = [query.strip()] + [v for v in cleaned if v.lower() != query.strip().lower()]
        return cleaned[:n]
    except Exception as exc:
        print(f"[multi_query] expand failed: {exc!r}")
        return [query]


def retrieve_candidates_multi(
    query: str,
    limit: int = 500,
    n_variants: int = DEFAULT_N_VARIANTS,
    **retrieve_kwargs,
) -> tuple[list[dict], list[str]]:
    """Run retrieve_candidates for each query variant and union the results
    keeping the max RRF score per commodity_code. Returns (candidates, variants_used).

    Pure wrapper - delegates each call to local_db.retrieve_candidates so it
    respects all the same flags (LOO exclusions, leg toggles, etc.).
    """
    variants = expand_query_to_variants(query, n=n_variants)
    if len(variants) == 1:
        # No real expansion - just call once and return
        cands = local_db.retrieve_candidates(variants[0], limit=limit, **retrieve_kwargs)
        return cands, variants

    seen: dict[str, dict] = {}
    for variant in variants:
        try:
            cands = local_db.retrieve_candidates(variant, limit=limit, **retrieve_kwargs)
        except Exception as exc:
            print(f"[multi_query] retrieval for variant {variant!r} failed: {exc!r}")
            continue
        for c in cands:
            code = c.get("commodity_code")
            if not code:
                continue
            if code not in seen or c["score"] > seen[code]["score"]:
                # Tag the candidate with which variants surfaced it
                surfaced_by = c.get("surfaced_by", [])
                if variant not in surfaced_by:
                    surfaced_by = list(surfaced_by) + [variant]
                c2 = dict(c)
                c2["surfaced_by"] = surfaced_by
                seen[code] = c2
            else:
                # Already have a higher-scored entry - just track the new surface
                existing = seen[code]
                surfaced = existing.get("surfaced_by", [])
                if variant not in surfaced:
                    existing["surfaced_by"] = surfaced + [variant]

    out = sorted(seen.values(), key=lambda x: -x["score"])
    return out[:limit], variants
