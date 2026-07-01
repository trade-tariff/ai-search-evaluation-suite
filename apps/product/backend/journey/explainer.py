"""Plain-English duty explainer.

Takes the full duty result (rate source, eligible preferences, excise breakdown,
KG edges in play) and asks the LLM to summarise it for a naive trader in 4-6
sentences. Grounded in actual numbers - no invented rates, no hallucinated
preferences.

If OPENAI_API_KEY is missing, falls back to a deterministic template that
strings the facts together. Same shape, less natural language.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .provider_guard import openai_allowed


def _llm_explain(payload: dict) -> Optional[str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_allowed() or not api_key:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        system = (
            "You are explaining UK import duty to a small trader who has just "
            "completed a guided calculation. Write 4-6 short sentences in plain "
            "English. Cover: what they're paying customs duty on, why this rate "
            "(MFN vs preference), any excise if applicable, and what proof of "
            "origin they need to hold on file. Stick strictly to the numbers in "
            "the JSON below - never invent rates, country names, or rules. No "
            "bullet points, no headings, no markdown. Use £ for money."
        )
        model = os.environ.get("EXPLAIN_LLM_MODEL", "gpt-5.5")
        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload)},
            ],
        }
        if not model.startswith("gpt-5"):
            kwargs["temperature"] = 0.3
        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:
        print(f"[explain LLM] {type(e).__name__}: {e}")
        return None


def _template_explain(payload: dict) -> str:
    """Deterministic fallback. Reads stiffly but says exactly the right things."""
    parts: list[str] = []
    rate = payload.get("rate_applied", 0)
    source = payload.get("rate_source", "MFN")
    duty = payload.get("customs_duty_gbp", 0)
    cv = payload.get("customs_value_gbp", 0)
    country = payload.get("country_name", payload.get("country_of_origin", "the country of origin"))
    code = payload.get("commodity_code", "")
    excise = payload.get("excise_duty_gbp", 0)

    parts.append(
        f"Your goods classified as {code} from {country} attract customs duty at {rate}% on the customs value of £{cv:.2f}, which is £{duty:.2f}."
    )

    if source == "MFN":
        parts.append(
            "This is the most-favoured-nation rate - no UK trade agreement offers a lower rate to this country for this product."
        )
    else:
        parts.append(
            f"This rate comes via the {source} preference, which is lower than the standard MFN rate. To claim it you need to hold a valid proof of origin (e.g. statement on invoice or REX number) - HMRC may ask to see it."
        )

    if excise > 0:
        parts.append(
            f"Because the goods are alcoholic, UK excise duty of £{excise:.2f} also applies on top of customs duty."
        )

    parts.append(
        "VAT applies on the customs value plus customs duty plus excise. The Landed stage adds it up so you see the cash you'll need to pay HMRC."
    )

    return " ".join(parts)


def explain_duty(payload: dict) -> str:
    return _llm_explain(payload) or _template_explain(payload)
