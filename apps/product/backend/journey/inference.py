"""Deterministic duty-input inference for the full-app e2e journey."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from . import local_db
from .duty import commodity_requirements
from .provider_guard import openai_allowed
from .schemas import DutyInputInferenceRequest, DutyInputInferenceResult


COUNTRY_WORDS = {
    "france": "FR",
    "germany": "DE",
    "italy": "IT",
    "spain": "ES",
    "netherlands": "NL",
    "ireland": "IE",
    "portugal": "PT",
    "japan": "JP",
    "australia": "AU",
    "new zealand": "NZ",
    "canada": "CA",
    "singapore": "SG",
    "vietnam": "VN",
    "viet nam": "VN",
    "mexico": "MX",
    "india": "IN",
    "bangladesh": "BD",
    "cambodia": "KH",
    "pakistan": "PK",
    "china": "CN",
    "united states": "US",
    "usa": "US",
    "turkey": "TR",
    "brazil": "BR",
    "south africa": "ZA",
}


def infer_duty_inputs(req: DutyInputInferenceRequest) -> DutyInputInferenceResult:
    base = _deterministic_infer(req)
    base, protected = _apply_known_inputs(req, base)
    mode = os.environ.get("JOURNEY_PREFILL_MODE", "deterministic").strip().lower()
    if mode in {"deterministic", "regex", "off"}:
        return base

    llm_payload = _llm_extract(req)
    if not llm_payload:
        return base
    merged = _merge_llm_prefill(req, base, llm_payload, protected)
    merged, _ = _apply_known_inputs(req, merged)
    return merged


def _deterministic_infer(req: DutyInputInferenceRequest) -> DutyInputInferenceResult:
    text = _combined_text(req)
    flat = re.sub(r"\D", "", req.commodity_code or "")
    requirements = commodity_requirements(flat)
    inferred: dict[str, Any] = {
        "import_destination": "GB",
        "vat_rate": requirements.default_vat_rate,
        "quantity_unit_type": requirements.supplementary_unit_type,
    }
    sources = {
        "import_destination": "GB path is the only implemented route in this app.",
        "vat_rate": "Commodity default VAT rate.",
    }
    skipped = ["destination", "vat"]
    warnings: list[str] = []

    country = _infer_country(text)
    if country:
        inferred["country_of_origin"] = country
        sources["country_of_origin"] = "Parsed country name from the trader's description."
        skipped.append("country")

    quantity = _infer_quantity(text, requirements.supplementary_unit_type, flat)
    if quantity is not None:
        inferred["quantity_units"] = round(quantity, 4)
        sources["quantity_units"] = "Parsed quantity from the trader's description."
        skipped.append("quantity")
    elif requirements.supplementary_unit_type:
        warnings.append(
            f"Could not infer supplementary units ({requirements.supplementary_unit_type}); trader must provide them."
        )

    abv = _infer_abv(text)
    volume_litres = _infer_volume_litres(text)
    if requirements.needs_excise:
        if abv is None:
            abv = _default_abv(flat)
            if abv is not None:
                sources["abv"] = "Suggested from beverage type because ABV was not in the description; trader must confirm."
                warnings.append(
                    "ABV was not stated by the trader. A beverage-type default was suggested but the ABV question must still be confirmed."
                )
        else:
            sources["abv"] = "Parsed alcohol by volume from the trader's description."
        if volume_litres is None:
            if requirements.supplementary_unit_type == "LTR" and quantity is not None:
                volume_litres = quantity
                sources["excise_volume_litres"] = "Supplementary litres are also the excise volume."
            elif flat.startswith("2208") and quantity is not None and abv:
                volume_litres = round(quantity / (abv / 100.0), 4)
                sources["excise_volume_litres"] = "Back-calculated from litres of pure alcohol and ABV."
        if abv is not None:
            inferred["abv"] = abv
            if "Suggested from beverage type" not in sources.get("abv", ""):
                skipped.append("excise_abv")
        if volume_litres is not None:
            inferred["excise_volume_litres"] = round(volume_litres, 4)
            sources.setdefault("excise_volume_litres", "Parsed bottle count and size from description.")
        small_producer = _infer_small_producer(text)
        if small_producer is not None:
            inferred["is_small_producer"] = small_producer
            sources["is_small_producer"] = "Parsed small-producer status from the trader's description."
            skipped.append("excise_spr")
        draught = _infer_draught(text)
        if draught is not None:
            inferred["is_draught"] = draught
            sources["is_draught"] = "Parsed draught/package status from the trader's description."
            skipped.append("excise_draught")

    if req.customs_value_gbp is not None:
        inferred["customs_value_gbp"] = req.customs_value_gbp
        sources["customs_value_gbp"] = "Carried forward from valuation stage."

    return DutyInputInferenceResult(
        inferred=inferred,
        sources=sources,
        skipped_questions=sorted(set(skipped)),
        warnings=warnings,
    )


def _llm_extract(req: DutyInputInferenceRequest) -> dict | None:
    if not openai_allowed() or not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI
    except Exception:
        return None

    text = _combined_text(req)
    flat = re.sub(r"\D", "", req.commodity_code or "")
    requirements = commodity_requirements(flat)
    commodity = local_db.commodity(flat) or {}
    countries = ", ".join(f"{c['code']}={c['name']}" for c in local_db.countries()[:80])
    model = os.environ.get("PREFILL_LLM_MODEL") or os.environ.get("CLASSIFY_LLM_MODEL") or "gpt-5-mini"

    system = (
        "Extract import-duty calculator prefills from a trader's text. "
        "Return JSON only. Use null when the text does not support a field. "
        "Do not calculate duty, VAT, preference rates, or customs value."
    )
    user = {
        "commodity_code": flat,
        "commodity_description": commodity.get("description", ""),
        "required_supplementary_unit": requirements.supplementary_unit_type,
        "needs_excise": requirements.needs_excise,
        "known_countries": countries,
        "text": text,
        "schema": {
            "country_of_origin": "ISO alpha-2 string or null",
            "quantity_units": "numeric quantity in the required_supplementary_unit, if directly inferable",
            "quantity_unit_type": "PR, LTR, LPA, NAR, KGM, or null",
            "excise_volume_litres": "litres of alcoholic product, if inferable",
            "abv": "alcohol by volume percentage, if inferable",
            "has_proof_of_origin": "boolean or null",
            "is_small_producer": "boolean or null",
            "is_draught": "boolean or null",
            "confidence": "object keyed by field name, 0.0 to 1.0",
            "notes": "short array of extraction notes",
        },
    }
    try:
        kwargs = {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user)},
            ],
        }
        temp = os.environ.get("PREFILL_LLM_TEMPERATURE")
        if temp is not None:
            kwargs["temperature"] = float(temp)
        resp = OpenAI().chat.completions.create(
            **kwargs,
        )
        content = resp.choices[0].message.content or "{}"
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _merge_llm_prefill(
    req: DutyInputInferenceRequest,
    base: DutyInputInferenceResult,
    llm: dict,
    protected: set[str],
) -> DutyInputInferenceResult:
    flat = re.sub(r"\D", "", req.commodity_code or "")
    requirements = commodity_requirements(flat)
    inferred = dict(base.inferred)
    sources = dict(base.sources)
    skipped = set(base.skipped_questions)
    warnings = list(base.warnings)
    confidence = llm.get("confidence") if isinstance(llm.get("confidence"), dict) else {}

    country = _normalize_country(llm.get("country_of_origin"))
    if "country_of_origin" not in protected and country and _conf_ok(confidence, "country_of_origin", default=0.65):
        inferred["country_of_origin"] = country
        sources["country_of_origin"] = "LLM prefill extractor, validated against country list."
        skipped.add("country")

    unit = _normalize_unit(llm.get("quantity_unit_type"))
    qty = _positive_float(llm.get("quantity_units"))
    excise_volume = _positive_float(llm.get("excise_volume_litres"))
    abv = _positive_float(llm.get("abv"))
    required_unit = requirements.supplementary_unit_type

    if required_unit == "LTR" and excise_volume is not None:
        qty = excise_volume
        unit = "LTR"
    elif required_unit == "LPA" and excise_volume is not None and abv is not None:
        qty = excise_volume * (abv / 100.0)
        unit = "LPA"

    if "quantity_units" not in protected and qty is not None and (required_unit is None or unit == required_unit):
        inferred["quantity_units"] = round(qty, 4)
        if required_unit:
            inferred["quantity_unit_type"] = required_unit
        sources["quantity_units"] = "LLM prefill extractor, validated against the commodity supplementary unit."
        skipped.add("quantity")
    elif qty is not None and required_unit:
        warnings.append(
            f"LLM proposed {qty} {unit or '(unknown unit)'}, but commodity requires {required_unit}; deterministic fallback kept."
        )

    if requirements.needs_excise:
        if "abv" not in protected and abv is not None and 0 < abv <= 100:
            inferred["abv"] = round(abv, 3)
            sources["abv"] = "LLM prefill extractor."
            skipped.add("excise_abv")
        if "excise_volume_litres" not in protected and excise_volume is not None:
            inferred["excise_volume_litres"] = round(excise_volume, 4)
            sources["excise_volume_litres"] = "LLM prefill extractor."
        for key, step in (("is_small_producer", "excise_spr"), ("is_draught", "excise_draught")):
            val = llm.get(key)
            if key not in protected and isinstance(val, bool):
                inferred[key] = val
                sources[key] = "LLM prefill extractor."
                skipped.add(step)

    proof = llm.get("has_proof_of_origin")
    if "has_proof_of_origin" not in protected and isinstance(proof, bool):
        inferred["has_proof_of_origin"] = proof
        sources["has_proof_of_origin"] = "LLM prefill extractor."
        skipped.add("proof")

    sources["prefill_mode"] = "LLM extraction with deterministic fallback."
    notes = llm.get("notes")
    if isinstance(notes, list):
        warnings.extend(str(n) for n in notes[:3] if n)

    return DutyInputInferenceResult(
        inferred=inferred,
        sources=sources,
        skipped_questions=sorted(skipped),
        warnings=warnings,
    )


def _apply_known_inputs(
    req: DutyInputInferenceRequest,
    result: DutyInputInferenceResult,
) -> tuple[DutyInputInferenceResult, set[str]]:
    known = req.known_inputs or {}
    inferred = dict(result.inferred)
    sources = dict(result.sources)
    skipped = set(result.skipped_questions)
    warnings = list(result.warnings)
    protected = {"import_destination", "vat_rate", "quantity_unit_type"}

    field_to_step = {
        "country_of_origin": "country",
        "quantity_units": "quantity",
        "abv": "excise_abv",
        "excise_volume_litres": "quantity",
        "has_proof_of_origin": "proof",
        "is_small_producer": "excise_spr",
        "is_draught": "excise_draught",
    }
    for field, step in field_to_step.items():
        if field not in known or known[field] in (None, ""):
            continue
        value = known[field]
        if field == "country_of_origin":
            value = _normalize_country(value)
            if not value:
                warnings.append("Known country_of_origin was ignored because it is not in the country list.")
                continue
        if field in {"quantity_units", "abv", "excise_volume_litres"}:
            value = _positive_float(value)
            if value is None:
                continue
            value = round(value, 4)
        if field in {"has_proof_of_origin", "is_small_producer", "is_draught"} and not isinstance(value, bool):
            continue
        inferred[field] = value
        sources[field] = "Already elicited earlier in the journey."
        skipped.add(step)
        protected.add(field)

    if req.customs_value_gbp is not None:
        protected.add("customs_value_gbp")

    return (
        DutyInputInferenceResult(
            inferred=inferred,
            sources=sources,
            skipped_questions=sorted(skipped),
            warnings=warnings,
        ),
        protected,
    )


def _combined_text(req: DutyInputInferenceRequest) -> str:
    parts = [req.query or ""]
    for turn in req.qa_history or []:
        parts.append(str(turn.get("question") or ""))
        parts.append(str(turn.get("answer") or ""))
    item = local_db.commodity(req.commodity_code) or {}
    parts.append(str(item.get("description") or ""))
    return " ".join(parts).lower()


def _infer_country(text: str) -> str | None:
    for name, code in sorted(COUNTRY_WORDS.items(), key=lambda x: -len(x[0])):
        if re.search(rf"\b{re.escape(name)}\b", text):
            return code
    code_match = re.search(r"\bfrom\s+([a-z]{2})\b", text)
    if code_match:
        code = code_match.group(1).upper()
        if any(c["code"] == code for c in local_db.countries()):
            return code
    return None


def _normalize_country(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if len(text) == 2:
        code = text.upper()
        return code if any(c["code"] == code for c in local_db.countries()) else None
    return _infer_country(text.lower())


def _normalize_unit(value: Any) -> str | None:
    if not value:
        return None
    raw = str(value).strip().upper()
    aliases = {
        "PAIR": "PR",
        "PAIRS": "PR",
        "LTRS": "LTR",
        "LITRE": "LTR",
        "LITRES": "LTR",
        "LITER": "LTR",
        "LITERS": "LTR",
        "ITEM": "NAR",
        "ITEMS": "NAR",
        "UNIT": "NAR",
        "UNITS": "NAR",
        "PURE_ALCOHOL_LITRES": "LPA",
    }
    return aliases.get(raw, raw if raw in {"PR", "NPR", "LTR", "LPA", "NAR", "KGM"} else None)


def _positive_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return out if out > 0 else None
    except Exception:
        return None


def _conf_ok(confidence: dict, key: str, default: float = 0.0) -> bool:
    try:
        return float(confidence.get(key, default)) >= 0.5
    except Exception:
        return default >= 0.5


def _infer_quantity(text: str, unit_type: str | None, flat_code: str) -> float | None:
    if not unit_type:
        return None
    bottle_volume = _infer_volume_litres(text)
    if unit_type == "LTR":
        if bottle_volume is not None:
            return bottle_volume
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:litres?|liters?|ltr|l)\b", text)
        return float(m.group(1)) if m else None
    if unit_type == "LPA":
        abv = _infer_abv(text)
        if bottle_volume is not None and abv is not None:
            return bottle_volume * (abv / 100.0)
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:lpa|litres?\s+of\s+pure\s+alcohol)\b", text)
        return float(m.group(1)) if m else None
    if unit_type in {"PR", "NPR"}:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:pairs?|prs?|pr)\b", text)
        return float(m.group(1)) if m else None
    if unit_type == "NAR":
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:items?|units?|pieces?)\b", text)
        return float(m.group(1)) if m else None
    if flat_code.startswith("7321"):
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:cookers?|hobs?|ovens?|appliances?)\b", text)
        return float(m.group(1)) if m else None
    return None


def _infer_volume_litres(text: str) -> float | None:
    bottle_count = re.search(r"(\d+(?:\.\d+)?)\s*(?:bottles?|cans?|cases?)\b", text)
    ml_size = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", text)
    litre_size = re.search(r"(\d+(?:\.\d+)?)\s*(?:litre|liter|ltr|l)\s*(?:each|bottles?|cans?)", text)
    if bottle_count and ml_size:
        return float(bottle_count.group(1)) * float(ml_size.group(1)) / 1000.0
    if bottle_count and litre_size:
        return float(bottle_count.group(1)) * float(litre_size.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:litres?|liters?|ltr)\b", text)
    return float(m.group(1)) if m else None


def _infer_abv(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:abv|alcohol)?", text)
    return float(m.group(1)) if m else None


def _default_abv(flat_code: str) -> float | None:
    if flat_code.startswith("2208"):
        return 40.0
    if flat_code.startswith("2203"):
        return 5.0
    if flat_code.startswith("2204"):
        return 12.5
    return None


def _infer_small_producer(text: str) -> bool | None:
    if re.search(r"\b(?:not|no)\s+(?:a\s+)?small\s+(?:producer|brewery|winery|distillery)\b", text):
        return False
    if re.search(r"\b(?:small\s+(?:producer|brewery|winery|distillery)|microbrewery|microdistillery)\b", text):
        return True
    return None


def _infer_draught(text: str) -> bool | None:
    if any(word in text for word in ("draught", "draft", "keg", "barrel", "cask")):
        return True
    if re.search(r"\b(?:bottles?|cans?)\b", text):
        return False
    return None
