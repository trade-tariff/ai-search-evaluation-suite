"""Session-time fact propagation.

Extracts structured facts from the user's current query + qa_history at
session time, normalising into our KG facet vocabulary where possible.

These ephemeral session facts then:
  1. Re-rank candidates (boost matches, penalise contradictions against
     each candidate's stored facets in kg.commodity_facets)
  2. Feed the classify prompt as a SESSION_FACTS section so the LLM treats
     user-asserted facts as hard constraints rather than soft hints

This closes the gap where user-volunteered information falls outside the
seeded facet vocabulary and gets dropped on the floor.

Honest about uncertainty: the LLM may hallucinate facts; we attach a
confidence score and the re-ranker applies low-confidence facts gently.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional

from . import local_db
from .provider_guard import openai_allowed

EXTRACTOR_MODEL = os.environ.get("SESSION_FACT_EXTRACTOR_MODEL", "gpt-5-mini")

_SYSTEM = """You extract structured facts from a UK trader's product description for the
purpose of customs classification. The trader may volunteer material, function,
form, brand, country of origin, intended use, etc.

You will be given the user query, any prior Q&A history, and a list of FACET
KEYS our knowledge graph already understands. Use one of the known keys
whenever possible. If the trader asserts something that doesn't fit, invent a
short snake_case key.

Output JSON only:
{
  "facts": [
    {"key": "...", "value": "...", "confidence": 0.0-1.0,
     "source_span": "the EXACT phrase from the user input that supports this"},
    ...
  ]
}

Rules:
- ONE fact per (key, value) pair. Don't repeat.
- Lowercase keys (snake_case) and values.
- DO NOT invent facts the user didn't assert. If the user says "shoes" do not
  add material:leather. If they say "red wine" do not add country:france.
- DO NOT extract negation or speculation ("not sure if it's leather" -> skip).
- confidence: 0.95 if the user said it directly, 0.7 if it's implied by context
  (e.g. "wine" implies beverage_type:wine), 0.5 or less for weak hints.
- If no facts are extractable, return {"facts": []}.
"""


@lru_cache(maxsize=1)
def _known_facet_keys() -> tuple[str, ...]:
    """Top facet keys present in the KG, to hint the LLM at our vocabulary."""
    try:
        with local_db._conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT facet_key FROM kg.commodity_facets GROUP BY 1 "
                "ORDER BY COUNT(*) DESC LIMIT 50"
            )
            return tuple(r["facet_key"] for r in cur.fetchall())
    except Exception:
        return ()


def extract_session_facts(
    query: str,
    qa_history: Optional[list[dict]] = None,
) -> list[dict]:
    """Run the LLM to extract facts from user input. Returns list of
    {key, value, confidence, source_span}. Empty list if no facts or LLM
    unavailable.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_allowed() or not api_key or not query.strip():
        return []
    try:
        from openai import OpenAI
    except Exception:
        return []

    known = _known_facet_keys()
    keys_hint = ", ".join(known[:40]) if known else "(none seeded yet)"
    history_text = "(no prior Q&A)"
    if qa_history:
        history_text = "\n".join(
            f"Q: {h.get('question','')}\nA: {h.get('answer','')}"
            for h in qa_history
        )

    user_prompt = (
        f"## Known facet keys (prefer these)\n{keys_hint}\n\n"
        f"## User query\n{query}\n\n"
        f"## Q&A so far\n{history_text}\n"
    )

    try:
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=EXTRACTOR_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
        )
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            return []
        data = json.loads(content)
    except Exception as exc:
        print(f"[session_facts] extraction failed: {exc!r}")
        return []

    facts = data.get("facts") or []
    cleaned: list[dict] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        key = (f.get("key") or "").strip().lower().replace(" ", "_")
        value = str(f.get("value") or "").strip().lower()
        if not key or not value:
            continue
        conf = f.get("confidence")
        try:
            conf = float(conf) if conf is not None else 0.7
        except Exception:
            conf = 0.7
        conf = max(0.0, min(1.0, conf))
        cleaned.append({
            "key": key,
            "value": value,
            "confidence": conf,
            "source_span": (f.get("source_span") or "").strip(),
        })
    return cleaned


# ---- Re-ranking ------------------------------------------------------

def _candidate_facet_lookup(codes: list[str]) -> dict[str, dict[str, set[str]]]:
    """Return classification-scoped {commodity_code: {facet_key: set(facet_value)}}."""
    if not codes:
        return {}
    lookup: dict[str, dict[str, set[str]]] = {c: {} for c in codes}
    try:
        with local_db._conn() as c, c.cursor() as cur:
            scope_filter = local_db._kg_use_scope_filter("cf", "commodity_facets", "classification")
            cur.execute(
                "SELECT cf.commodity_code, cf.facet_key, cf.facet_value "
                "FROM kg.commodity_facets cf "
                f"WHERE cf.commodity_code = ANY(%s) {scope_filter}",
                (codes,),
            )
            for r in cur.fetchall():
                code = r["commodity_code"]
                key = r["facet_key"]
                val = str(r["facet_value"]).lower()
                if code in lookup:
                    lookup[code].setdefault(key, set()).add(val)
    except Exception as exc:
        print(f"[session_facts] candidate facet lookup failed: {exc!r}")
    return lookup


# Some facet keys are inherently multi-valued (a wine can have several
# designations; a shoe can have multiple accessories). Contradiction logic
# only applies to single-value facets where one true value rules others out.
_SINGLE_VALUED = {
    "material_upper", "material_sole", "material", "body_material",
    "still_or_sparkling", "alcohol_band", "wine_color", "beverage_type",
    "container_size", "country_of_origin", "form", "ankle_covered",
    "construction", "closure", "intended_use", "alcohol_strength",
    "wine_style", "wine_type",
}


# Cross-key value sharing: when the session says "material=plastic" but the
# candidate facet key is "material_upper" or "material_sole", we should count
# that as a soft match. These groups share a value space.
_KEY_GROUPS = {
    "material": ("material", "material_upper", "material_sole", "body_material"),
    "material_upper": ("material", "material_upper"),
    "material_sole": ("material", "material_sole"),
    "body_material": ("material", "body_material"),
    "origin": ("origin", "country_of_origin", "region"),
    "country_of_origin": ("origin", "country_of_origin"),
    "product_type": ("product_type", "sub_type", "article_type", "common_term"),
    "common_term": ("common_term", "product_type", "sub_type"),
}


def _norm_value(v: str) -> str:
    """Normalise a facet value for comparison: lowercase + strip punctuation."""
    return "".join(ch for ch in (v or "").lower() if ch.isalnum())


def _values_match(a: str, b: str) -> bool:
    """Looser comparison: normalised substring match either way."""
    na, nb = _norm_value(a), _norm_value(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 4 and (na in nb or nb in na):
        return True
    return False


def rerank_with_session_facts(
    candidates: list[dict],
    session_facts: list[dict],
    boost_per_match: float = 0.08,
    penalty_per_contradiction: float = 0.20,
) -> list[dict]:
    """Apply session facts as a re-ranker on top of the RRF-fused candidates.

    Matching is forgiving by design:
      - Exact (key, value) match: full boost
      - Cross-key match within a _KEY_GROUPS family: 0.7x boost
      - Substring-normalised value match: counts as match
      - Contradiction (same key, single-valued, different value): penalty

    Penalties are heavier than boosts because a contradiction is stronger
    evidence than a match (the user EXPLICITLY said X, candidate says Y).
    """
    if not candidates or not session_facts:
        for c in candidates:
            c["session_fact_matches"] = []
            c["session_fact_contradictions"] = []
            c["session_score_delta"] = 0.0
        return candidates

    codes = [c["commodity_code"] for c in candidates]
    cand_facets = _candidate_facet_lookup(codes)

    out: list[dict] = []
    for c in candidates:
        code = c["commodity_code"]
        cand = cand_facets.get(code, {})
        matches: list[dict] = []
        contradictions: list[dict] = []
        delta = 0.0
        for sf in session_facts:
            key = sf["key"]
            value = sf["value"]
            conf = sf["confidence"]

            # 1. Exact key match
            stored_exact = cand.get(key, set())
            if stored_exact:
                if any(_values_match(value, sv) for sv in stored_exact):
                    matches.append({**sf, "via": "exact_key", "candidate_values": list(stored_exact)})
                    delta += boost_per_match * conf
                    continue
                elif key in _SINGLE_VALUED:
                    contradictions.append({**sf, "via": "exact_key", "candidate_values": list(stored_exact)})
                    delta -= penalty_per_contradiction * conf
                    continue
            # 2. Sibling-key match (within group)
            sibling_keys = _KEY_GROUPS.get(key, ())
            sibling_hit = False
            for sk in sibling_keys:
                if sk == key:
                    continue
                stored_sk = cand.get(sk, set())
                if stored_sk and any(_values_match(value, sv) for sv in stored_sk):
                    matches.append({**sf, "via": f"sibling:{sk}", "candidate_values": list(stored_sk)})
                    delta += 0.7 * boost_per_match * conf
                    sibling_hit = True
                    break
            if sibling_hit:
                continue
            # Otherwise: no signal, neutral

        new_c = dict(c)
        new_c["session_fact_matches"] = matches
        new_c["session_fact_contradictions"] = contradictions
        new_c["session_score_delta"] = round(delta, 6)
        new_c["score"] = c["score"] + delta
        out.append(new_c)

    out.sort(key=lambda x: -x["score"])
    return out


# ---- Prompt section --------------------------------------------------

def session_facts_prompt_section(session_facts: list[dict]) -> str:
    """Render session facts for the LLM prompt. The LLM is told to treat
    these as HARD constraints (the user asserted them) rather than hints.
    """
    if not session_facts:
        return "(no user-asserted facts extracted)"
    lines = ["The following facts were asserted by the trader in their input or "
             "Q&A history. Treat these as HARD CONSTRAINTS - do not pick a "
             "commodity that contradicts them. Confidence is the LLM's extraction "
             "confidence; >=0.8 = explicit, <0.6 = implied.\n"]
    for f in session_facts:
        span = f" (from: \"{f['source_span']}\")" if f.get("source_span") else ""
        lines.append(f"- {f['key']} = {f['value']}  [conf {f['confidence']:.2f}]{span}")
    return "\n".join(lines)
