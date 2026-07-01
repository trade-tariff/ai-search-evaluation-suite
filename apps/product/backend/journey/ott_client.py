"""Thin client over the OTT public JSON API.

Uses the live service at https://www.trade-tariff.service.gov.uk/api/v2/...
The same data is also in the local tariff_db when running. The local DB is
preferred when available; otherwise the live API is used (10-15s timeouts).

The OTT response is JSON:API with relationships and an `included` array. We
flatten the bits we need into simple dicts so the rest of the backend never
has to deal with JSON:API directly.
"""
from __future__ import annotations

import os
import re
from datetime import date
from functools import lru_cache
from typing import Any, Optional

import httpx

OTT_BASE_URL = os.environ.get("OTT_BASE_URL", "https://www.trade-tariff.service.gov.uk/api/v2")
OTT_TIMEOUT_S = float(os.environ.get("OTT_TIMEOUT_S", "8"))

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def _strip_html(s: Optional[str]) -> str:
    if not s:
        return ""
    text = _TAG_RE.sub("", s)
    return _SPACE_RE.sub(" ", text).strip()


def _flatten_dotted(code: str) -> str:
    """6402.20.00 -> 6402200000. The OTT API uses unpunctuated 10-digit codes."""
    digits = re.sub(r"\D", "", code)
    return digits.ljust(10, "0")[:10] if digits else code


def _add_dots(code: str) -> str:
    """6402200000 -> 6402.20.00 (3-digit grouping after the heading)."""
    digits = re.sub(r"\D", "", code)
    if len(digits) < 6:
        return code
    return f"{digits[:4]}.{digits[4:6]}.{digits[6:8] if len(digits) >= 8 else ''}".rstrip(".")


@lru_cache(maxsize=256)
def _get(path: str, params_tuple: tuple) -> dict:
    """Cached JSON GET. params_tuple is a tuple of (key, value) for cacheability."""
    params = dict(params_tuple)
    with httpx.Client(timeout=OTT_TIMEOUT_S) as client:
        r = client.get(f"{OTT_BASE_URL}{path}", params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()


# --- Search ------------------------------------------------------------

def search(query: str, limit: int = 12) -> list[dict]:
    """Fuzzy search the OTT for the trader's query.

    Returns a list of {code, description, score, source} sorted by relevance.
    Combines goods_nomenclature_match (direct hits) with reference_match
    (curated alias hits) into a single ranked list.
    """
    if not query.strip():
        return []
    try:
        d = _get("/search", (("q", query.strip()),))
    except Exception as e:
        print(f"[ott search] {type(e).__name__}: {e}")
        return []

    attrs = d.get("data", {}).get("attributes", {})
    out: list[dict] = []
    seen: set[str] = set()

    # Reference match (curated aliases like "flip-flops") - usually very accurate
    ref = attrs.get("reference_match", {}) or {}
    for level in ("commodities", "headings", "chapters"):
        for hit in ref.get(level, []) or []:
            src = hit.get("_source", {}) or {}
            ref_obj = src.get("reference") or {}
            code = ref_obj.get("goods_nomenclature_item_id")
            if not code or code in seen:
                continue
            seen.add(code)
            out.append({
                "code": code,
                "code_dotted": _add_dots(code),
                "description": ref_obj.get("description") or src.get("title") or "",
                "score": float(hit.get("_score", 0)),
                "source": "reference",
            })

    # Direct goods nomenclature match
    gnm = attrs.get("goods_nomenclature_match", {}) or {}
    for level in ("commodities", "headings", "chapters"):
        for hit in gnm.get(level, []) or []:
            src = hit.get("_source", {}) or {}
            code = src.get("goods_nomenclature_item_id")
            if not code or code in seen:
                continue
            seen.add(code)
            out.append({
                "code": code,
                "code_dotted": _add_dots(code),
                "description": src.get("description") or "",
                "score": float(hit.get("_score", 0)),
                "source": "nomenclature",
            })

    out.sort(key=lambda x: -x["score"])
    return out[:limit]


# --- Commodity details + applicable measures --------------------------

def commodity_with_measures(code: str, country_code: Optional[str] = None) -> Optional[dict]:
    """Return commodity attributes + a list of measures that apply to (code, country).

    If country_code is None, returns all ERGA OMNES measures (i.e. MFN + VAT only).
    """
    flat = _flatten_dotted(code)
    params: tuple = ()
    if country_code:
        params = (("filter[geographical_area_id]", country_code),)
    try:
        d = _get(f"/commodities/{flat}", params)
    except Exception as e:
        print(f"[ott commodity] {type(e).__name__}: {e}")
        return None

    data = d.get("data", {})
    attrs = data.get("attributes", {})
    included = d.get("included", [])

    measure_components = {i["id"]: i["attributes"] for i in included if i["type"] == "measure_component"}
    duty_expressions = {i["id"]: i["attributes"] for i in included if i["type"] == "duty_expression"}
    geo_areas = {i["id"]: i["attributes"] for i in included if i["type"] == "geographical_area"}
    measure_types = {i["id"]: i["attributes"] for i in included if i["type"] == "measure_type"}

    measures: list[dict] = []
    for m in included:
        if m["type"] != "measure":
            continue
        a = m["attributes"]
        rels = m["relationships"]
        mt_id = rels.get("measure_type", {}).get("data", {}).get("id")
        ga_id = rels.get("geographical_area", {}).get("data", {}).get("id")
        de_id = (rels.get("duty_expression", {}).get("data") or {}).get("id")
        de = duty_expressions.get(de_id, {}) if de_id else {}
        mc_data = rels.get("measure_components", {}).get("data") or []
        components = [measure_components.get(c["id"], {}) for c in mc_data]
        measures.append({
            "measure_id": m["id"],
            "measure_type_id": mt_id,
            "measure_type_description": (measure_types.get(mt_id, {}) or {}).get("description", ""),
            "geographical_area_id": ga_id,
            "geographical_area_description": (geo_areas.get(ga_id, {}) or {}).get("description", ""),
            "duty_expression_html": de.get("formatted_base", ""),
            "duty_expression_text": _strip_html(de.get("formatted_base", "")),
            "components": components,
            "vat": a.get("vat", False),
            "import": a.get("import", True),
            "effective_start_date": a.get("effective_start_date"),
            "effective_end_date": a.get("effective_end_date"),
        })

    return {
        "code": flat,
        "code_dotted": _add_dots(flat),
        "description": attrs.get("description") or attrs.get("formatted_description") or "",
        "formatted_description": _strip_html(attrs.get("formatted_description") or attrs.get("description") or ""),
        "supplementary_measure_unit": attrs.get("supplementary_measure_unit"),
        "measures": measures,
    }


# --- Best applicable duty rate ----------------------------------------

# Measure-type IDs we care about for the POC. Real OTT covers many more.
MFN_TYPE = "103"
PREFERENCE_TYPE = "142"
VAT_TYPE = "305"
SUPPLEMENTARY_TYPE = "109"
SUPPLEMENTARY_TYPE_ALT = "110"


def _measure_rate(measure: dict) -> Optional[dict]:
    """Inspect a measure's first component and decide whether it's ad valorem
    (a percentage) or a specific duty (e.g. £26 per hectolitre).

    Returns a dict:
      { kind: 'ad_valorem' | 'specific' | 'free',
        rate_pct: float | None,
        amount: float | None,
        per_unit: str | None,
        monetary_unit: str | None,
        text: str }
    or None if no rate could be parsed.
    """
    text = measure.get("duty_expression_text") or ""
    components = measure.get("components") or []
    if not components:
        return None
    c = components[0]
    amt = c.get("duty_amount")
    if amt is None:
        # "Free" measures often have no amount but a duty_expression of "0.00%" via the duty_expression record.
        # We treat them as 0% ad valorem.
        if "%" in text and "0" in text:
            return {"kind": "ad_valorem", "rate_pct": 0.0, "amount": 0.0, "per_unit": None, "monetary_unit": None, "text": text}
        return None
    mu = c.get("measurement_unit_code")
    monu = c.get("monetary_unit_code")
    # If the component carries a measurement unit + monetary unit, it's specific (X currency per unit-of-measure).
    if mu and monu:
        return {
            "kind": "specific",
            "rate_pct": None,
            "amount": float(amt),
            "per_unit": mu,
            "monetary_unit": monu,
            "text": text,
        }
    # Plain numeric without units = ad valorem percentage.
    return {
        "kind": "ad_valorem",
        "rate_pct": float(amt),
        "amount": float(amt),
        "per_unit": None,
        "monetary_unit": None,
        "text": text,
    }


def best_applicable_duty(code: str, country_code: str, date_iso: Optional[str] = None) -> dict:
    """Pull the real measures and pick the best applicable customs duty rate.

    Returns a dict with:
      - mfn: rate-dict (see _measure_rate) or None
      - preference: rate-dict + geo info, or None
      - vat_rate: float (UK default 20)
      - supplementary_unit_code: str or None
      - all_measures: raw list (for the UI to expand)
    """
    bundle = commodity_with_measures(code, country_code)
    if bundle is None:
        return {
            "mfn": None,
            "preference": None,
            "vat_rate": None,
            "supplementary_unit_code": None,
            "all_measures": [],
            "error": "OTT API unavailable",
        }

    measures = bundle["measures"]
    mfn: Optional[dict] = None
    best_pref: Optional[dict] = None
    vat_rate: Optional[float] = None
    sup_code: Optional[str] = bundle.get("supplementary_measure_unit") or None

    for m in measures:
        mt = m["measure_type_id"]
        rate = _measure_rate(m)

        if mt == MFN_TYPE and rate is not None:
            # For ad valorem, take the lowest; for specific, take the first (rarely competes).
            if mfn is None or (rate["kind"] == "ad_valorem" and mfn["kind"] == "ad_valorem"
                               and (rate["rate_pct"] or 0) < (mfn["rate_pct"] or 0)):
                mfn = rate

        if mt == PREFERENCE_TYPE and rate is not None:
            candidate = {
                **rate,
                "measure_id": m["measure_id"],
                "geographical_area_id": m["geographical_area_id"],
                "geographical_area_description": m["geographical_area_description"],
            }
            # Pick the lowest preferential rate, both kinds comparable as long as same shape.
            def _pref_score(r: dict) -> float:
                if r["kind"] == "free":
                    return 0
                if r["kind"] == "ad_valorem":
                    return float(r.get("rate_pct") or 0)
                # Specific - use the per-unit amount as the score (rough; may not be apples-to-apples but ok for the POC)
                return float(r.get("amount") or 0)
            if best_pref is None or _pref_score(rate) < _pref_score(best_pref):
                best_pref = candidate

        if mt == VAT_TYPE and rate is not None and rate["kind"] == "ad_valorem":
            v = rate.get("rate_pct")
            if v is not None and (vat_rate is None or v > vat_rate):
                vat_rate = v

        if mt in (SUPPLEMENTARY_TYPE, SUPPLEMENTARY_TYPE_ALT) and not sup_code:
            for c in m.get("components") or []:
                if c.get("measurement_unit_code"):
                    sup_code = c["measurement_unit_code"]
                    break

    return {
        "code": bundle["code"],
        "code_dotted": bundle["code_dotted"],
        "description": bundle["formatted_description"] or bundle["description"],
        "mfn": mfn,
        "preference": best_pref,
        "vat_rate": vat_rate,
        "supplementary_unit_code": sup_code,
        "all_measures": measures,
    }
