"""Duty calculation (GB import path), powered by the local tariff DB.

Reflects the OTT duty-calculator step shape. Customs duty rate comes from the
real `uk.measures` + `uk.measure_components` (resolved through geographical
group memberships). Excise stays local (UK Alcohol Duty Reform 2023).

Works for any of the ~25k active UK commodities. The hand-authored facets/KG
slice does not gate this stage.
"""
from __future__ import annotations

import re
from typing import Optional

from . import local_db
from .excise import calculate_excise
from .meursing import resolve_meursing
from .schemas import (
    CommodityRequirements,
    DutyRequest,
    DutyResult,
    ExciseBreakdown,
)


# --- Public helpers (used by main.py) ----------------------------------

def country_list() -> list[dict]:
    """All active 2-letter UK tariff geographical areas. Used by the FE picker."""
    raw = local_db.countries()
    # Backfill with each country's current group memberships so the FE can
    # show "France (FR) - EU members, EU TCA, ..." in the select.
    enriched: list[dict] = []
    for c in raw[:300]:  # cap for select rendering
        groups = local_db.country_groups(c["code"])
        enriched.append({
            "code": c["code"],
            "name": c["name"],
            "groups": [g["code"] for g in groups][:5],
            "group_names": [g["name"] for g in groups][:5],
        })
    return enriched


def commodity_lookup(code: str) -> Optional[dict]:
    return local_db.commodity(code)


def commodity_requirements(code: str) -> CommodityRequirements:
    """Decide which substeps the duty wizard needs to show for this commodity.

    All inputs from the live DB except the excise category mapping, which we
    infer from the chapter/heading: alcohol (ch 22), tobacco (ch 24), road
    fuel (2710). Non-alcohol Chapter 22 entries (waters, juices) skip excise.
    """
    flat = re.sub(r"\D", "", code or "")
    chapter = flat[:2]

    sup_code = local_db.supplementary_unit_for(code)

    needs_excise = False
    excise_category = None
    beverage_type = None
    required_inputs: list[str] = []
    if chapter == "22" and not flat.startswith(("2201", "2202", "2009")):
        # We treat any 2204/2206/2208/2203 commodity as alcoholic for the
        # excise wizard. Waters/juices (2201/2202) skip excise.
        needs_excise = True
        excise_category = "alcohol"
        beverage_type = _infer_beverage_type(flat)
        required_inputs = ["abv", "volume_litres"]
    elif _tobacco_category(flat):
        needs_excise = True
        excise_category = "tobacco"
        required_inputs = (
            ["sticks", "retail_value_gbp"]
            if _tobacco_category(flat) == "cigarettes"
            else ["net_weight_kg"]
        )
    elif _fuel_covered(flat):
        needs_excise = True
        excise_category = "fuel"
        required_inputs = ["volume_litres"]

    needs_meursing = _needs_meursing_check(flat)
    db_vat = local_db.commodity_vat_rate(code)

    return CommodityRequirements(
        needs_supplementary_units=bool(sup_code),
        supplementary_unit_type=sup_code,
        needs_excise=needs_excise,
        excise_beverage_type=beverage_type,
        excise_category=excise_category,
        excise_required_inputs=required_inputs,
        needs_meursing_code=needs_meursing,
        additional_code_type="Meursing" if needs_meursing else None,
        mfn_rate=0.0,
        has_any_preference=True,
        default_vat_rate=db_vat if db_vat is not None else 20.0,
    )


def _infer_beverage_type(flat_code: str) -> str:
    if flat_code.startswith("2204"):
        return "wine"
    if flat_code.startswith("2206"):
        return "wine"  # other fermented beverages - same band rule
    if flat_code.startswith("2208"):
        return "spirits"
    if flat_code.startswith("2203"):
        return "beer"
    return "wine"


def _needs_meursing_check(flat_code: str) -> bool:
    """Processed-agri headings where a Meursing/additional-code check is plausible.

    GB UKGT rates are simplified, but NI/EU-style declaration handoffs can still
    require the composition-derived code. Keep this broad enough for the demo
    without asking it for unrelated goods.
    """
    return flat_code.startswith((
        "1704",  # sugar confectionery
        "1806",  # chocolate and other cocoa preparations
        "1901", "1902", "1904", "1905",  # cereal/bakery preparations
        "2004", "2005",  # prepared vegetables
        "2101", "2103", "2105", "2106",  # misc food preparations
    ))


# --- Tobacco + hydrocarbon fuel excise ----------------------------------
#
# Rates as of 2026 - hard-coded demo snapshot of the published GOV.UK rate
# tables (Tobacco Products Duty, Fuel Duty). Verify the current figures on
# GOV.UK before relying on them for a real import.

_TOBACCO_RATES_2026 = {
    "cigarettes_per_1000": 316.70,      # GBP per 1,000 sticks
    "cigarettes_ad_valorem_pct": 16.5,  # % of UK retail selling price
    "cigars_per_kg": 395.03,
    "hand_rolling_per_kg": 412.32,
    "other_smoking_per_kg": 173.68,
    "heating_per_kg": 325.53,
}
_ROAD_FUEL_RATE_2026 = 0.5295           # GBP per litre, petrol/diesel main rate

_ROAD_FUEL_PREFIXES = (
    "271012",                                        # light oils incl. motor spirit (petrol)
    "27101943", "27101946", "27101947", "27101948",  # gas oil incl. road diesel
    "271020",                                        # petroleum oils containing biodiesel
)


def _tobacco_category(flat_code: str) -> Optional[str]:
    if flat_code.startswith(("240220", "240290")):
        return "cigarettes"
    if flat_code.startswith("240210"):
        return "cigars"
    if flat_code.startswith("240411"):
        return "heating"
    if flat_code.startswith("2403"):
        return "other_smoking"
    return None


def _fuel_covered(flat_code: str) -> bool:
    return flat_code.startswith(_ROAD_FUEL_PREFIXES)


def _warn(kind: str, message: str, rate_range: Optional[str] = None) -> dict:
    return {"kind": kind, "message": message, "rate_range": rate_range}


def _db_excise_rates(flat_code: str, import_date: Optional[str]) -> Optional[dict]:
    """Live excise rates from the local tariff DB's type-306 measure components.

    Maps the component rows onto the rate slots the calculators use; None when
    the DB has nothing (callers fall back to the static 2026 snapshot).
    Duty expression 15 carries the cigarette Minimum Excise Tax floor.
    """
    comps = getattr(local_db, "excise_rate_components", lambda *a, **k: [])(
        flat_code, as_of=import_date
    )
    if not comps:
        return None
    out: dict = {
        "ret_pct": None, "per_mil": None, "met_per_mil": None,
        "per_kg": None, "per_ltr": None,
        "attached_at": comps[0].get("attached_at"),
        "additional_code": comps[0].get("additional_code"),
    }
    for c in comps:
        unit = (c.get("measurement_unit_code") or "").strip()
        expr = str(c.get("duty_expression_id") or "").strip()
        amt = float(c["duty_amount"])
        if unit == "RET":
            out["ret_pct"] = amt
        elif unit == "MIL":
            if expr == "15":
                out["met_per_mil"] = amt
            else:
                out["per_mil"] = amt
        elif unit == "KGM" and expr != "15":
            out["per_kg"] = amt
        elif unit == "LTR" and expr != "15":
            out["per_ltr"] = amt
    return out


def _excise_gap_warning(flat_code: str) -> Optional[dict]:
    """Chapter 24/27 sub-cases the local excise model deliberately skips."""
    if flat_code.startswith("2401"):
        return _warn(
            "excise_missing",
            "Raw/unmanufactured tobacco is not covered by the local excise model - "
            "tobacco duty and the Raw Tobacco Approval Scheme may still apply. Verify on GOV.UK.",
        )
    if flat_code.startswith(("240412", "240419")):
        return _warn(
            "excise_missing",
            "Vaping/nicotine products are not covered by the local excise model "
            "(Vaping Products Duty applies from October 2026). Verify on GOV.UK.",
        )
    if flat_code.startswith("2710") and not _fuel_covered(flat_code):
        return _warn(
            "excise_missing",
            "This oil is outside the local fuel-duty model (kerosene, fuel oil and lubricants "
            "have different or rebated rates). Verify hydrocarbon oil duty on GOV.UK.",
        )
    if flat_code.startswith("2711"):
        return _warn(
            "excise_missing",
            "Gaseous fuels (LPG/CNG) have their own per-kg fuel duty rates - "
            "not covered by the local excise model. Verify on GOV.UK.",
        )
    return None


def _quantity_litres(req: DutyRequest) -> Optional[float]:
    if req.excise_inputs is not None and req.excise_inputs.volume_litres:
        return float(req.excise_inputs.volume_litres)
    if req.quantity_units:
        factor = {"LTR": 1.0, "HLT": 100.0, "MTQ": 1000.0}.get(req.quantity_unit_type or "")
        if factor:
            return float(req.quantity_units) * factor
    return None


def _quantity_kg(req: DutyRequest) -> Optional[float]:
    if req.excise_inputs is not None and req.excise_inputs.net_weight_kg:
        return float(req.excise_inputs.net_weight_kg)
    if req.quantity_units and req.quantity_unit_type == "KGM":
        return float(req.quantity_units)
    if req.quantity_units and req.quantity_unit_type == "TNE":
        return float(req.quantity_units) * 1000.0
    return None


def _quantity_sticks(req: DutyRequest) -> Optional[float]:
    if req.excise_inputs is not None and req.excise_inputs.sticks:
        return float(req.excise_inputs.sticks)
    if req.quantity_units and req.quantity_unit_type == "MIL":
        return float(req.quantity_units) * 1000.0
    return None


def _calculate_tobacco_excise(flat_code: str, req: DutyRequest, warnings: list[dict]) -> tuple[float, Optional[dict]]:
    """Tobacco Products Duty for the main chapter-24 categories.

    Returns (duty_gbp, excise_detail). Missing inputs degrade to warnings
    (kind 'excise_missing') rather than blocking the calculation.
    """
    category = _tobacco_category(flat_code)
    components: list[dict] = []
    detail_notes: list[str] = []
    total = 0.0
    db = _db_excise_rates(flat_code, req.import_date)
    rates_as_of = "live UK tariff data" if db else "2026 snapshot"

    if category == "cigarettes":
        label = "Cigarettes"
        if flat_code.startswith("240290"):
            warnings.append(_warn(
                "excise_missing",
                "Code 2402 90 covers cigars/cigarillos/cigarettes of tobacco substitutes - "
                "cigarette rates assumed; verify the duty category on GOV.UK.",
            ))
        sticks = _quantity_sticks(req)
        if sticks is None:
            warnings.append(_warn(
                "excise_missing",
                "Tobacco duty on cigarettes needs the number of sticks (per-1,000 element) - "
                "excise is NOT included in the total. Supply sticks or a quantity in MIL (thousand items).",
            ))
            return 0.0, None
        per_1000 = (db or {}).get("per_mil") or _TOBACCO_RATES_2026["cigarettes_per_1000"]
        specific = round(per_1000 * sticks / 1000.0, 2)
        components.append({
            "label": f"GBP {per_1000:.2f} per 1,000 cigarettes x {sticks:,.0f} sticks",
            "amount_gbp": specific,
        })
        total += specific
        retail = req.excise_inputs.retail_value_gbp if req.excise_inputs else None
        ad_valorem_pct = (db or {}).get("ret_pct") or _TOBACCO_RATES_2026["cigarettes_ad_valorem_pct"]
        if retail is None:
            warnings.append(_warn(
                "excise_missing",
                f"The {ad_valorem_pct}% ad valorem element of cigarette duty needs the UK retail "
                "selling price - only the per-1,000-sticks element is included in the total.",
            ))
        else:
            ad_valorem = round(float(retail) * ad_valorem_pct / 100.0, 2)
            components.append({
                "label": f"{ad_valorem_pct}% of GBP {float(retail):.2f} retail selling price",
                "amount_gbp": ad_valorem,
            })
            total += ad_valorem
        met = (db or {}).get("met_per_mil")
        if met:
            floor = round(met * sticks / 1000.0, 2)
            if total < floor:
                components.append({
                    "label": f"Minimum Excise Tax: GBP {met:.2f} per 1,000 floor tops up the calculated duty",
                    "amount_gbp": round(floor - total, 2),
                })
                total = floor
                detail_notes.append(
                    "Minimum Excise Tax applied - the calculated duty was below the per-1,000 floor."
                )
        else:
            detail_notes.append(
                "Minimum Excise Tax for low-priced cigarettes is not modelled - the real duty can be higher."
            )
    elif category in ("cigars", "other_smoking", "heating"):
        rate_key, label = {
            "cigars": ("cigars_per_kg", "Cigars"),
            "other_smoking": ("other_smoking_per_kg", "Other smoking/chewing tobacco"),
            "heating": ("heating_per_kg", "Tobacco for heating"),
        }[category]
        kg = _quantity_kg(req)
        if kg is None:
            warnings.append(_warn(
                "excise_missing",
                f"Tobacco duty on {label.lower()} is charged per kg - supply the net tobacco weight "
                "to include it. Excise is NOT included in the total.",
            ))
            return 0.0, None
        rate = (db or {}).get("per_kg") or _TOBACCO_RATES_2026[rate_key]
        total = round(rate * kg, 2)
        components.append({"label": f"GBP {rate:.2f} per kg x {kg:g} kg", "amount_gbp": total})
        if category == "other_smoking" and not (db or {}).get("per_kg"):
            detail_notes.append(
                f"Hand-rolling (fine-cut) tobacco is GBP {_TOBACCO_RATES_2026['hand_rolling_per_kg']:.2f}/kg "
                f"rather than the GBP {rate:.2f}/kg used here - check which duty category applies."
            )
    else:
        return 0.0, None

    total = round(total, 2)
    return total, {
        "category": "tobacco",
        "label": label,
        "components": components,
        "notes": detail_notes,
        "rates_as_of": rates_as_of,
        "duty_gbp": total,
    }


def _calculate_fuel_excise(flat_code: str, req: DutyRequest, warnings: list[dict]) -> tuple[float, Optional[dict]]:
    """Fuel Duty per litre - rate from the live tariff DB's excise measure for
    this exact code when available, else the static main-rate snapshot."""
    litres = _quantity_litres(req)
    if litres is None:
        warnings.append(_warn(
            "excise_missing",
            "Fuel duty is charged per litre - supply the volume in litres (or a quantity in "
            "LTR/MTQ) to include it. Excise is NOT included in the total.",
        ))
        return 0.0, None
    db = _db_excise_rates(flat_code, req.import_date)
    rate = (db or {}).get("per_ltr") or _ROAD_FUEL_RATE_2026
    total = round(rate * litres, 2)
    if db and db.get("per_ltr"):
        label = "Hydrocarbon oil (rate for this commodity code)"
        notes = ["Per-litre rate taken from the excise measure on this commodity in the UK tariff data."]
        rates_as_of = "live UK tariff data"
    else:
        label = "Hydrocarbon oil (petrol/diesel main rate)"
        notes = [
            "Main road-fuel rate assumed. Rebated uses (red diesel, heating kerosene) "
            "have lower or nil rates - not modelled.",
        ]
        rates_as_of = "2026 snapshot"
    return total, {
        "category": "fuel",
        "label": label,
        "components": [{
            "label": f"GBP {rate:.4f} per litre x {litres:g} litres",
            "amount_gbp": total,
        }],
        "notes": notes,
        "rates_as_of": rates_as_of,
        "duty_gbp": total,
    }


# --- Main calc ---------------------------------------------------------

# Measure-type families for the educational annotate-and-warn layer.
_RELIEF_TYPES = {"112", "115", "122", "143"}  # suspensions + tariff quotas
_QUOTA_TYPES = {"122", "143"}
_REMEDY_FAMILIES = (
    ("Anti-dumping duty", ("551", "552")),
    ("Countervailing duty", ("553", "554")),
    ("Safeguard/additional duty", ("695", "696")),
)
_REMEDY_PENDING_TYPE = "555"  # AD/CVD pending collection - warn only
_PROHIBITION_TYPES = {"277", "705"}  # import prohibitions
_RESTRICTION_TYPES = {"465", "475", "481", "711", "722"}  # import restrictions/controls

# Unit-of-measure conversions for applying specific duty against the trader's quantity.
_UNIT_CONVERSIONS = {
    ("LTR", "HLT"): 0.01,
    ("HLT", "LTR"): 100.0,
    ("LTR", "LTR"): 1.0,
    ("PR", "PR"): 1.0,
    ("PR", "NPR"): 1.0,
    ("NPR", "PR"): 1.0,
    ("NPR", "NPR"): 1.0,
    ("KGM", "KGM"): 1.0,
    ("KGM", "TNE"): 0.001,
    ("TNE", "KGM"): 1000.0,
}


def _convert_quantity(qty: Optional[float], from_unit: Optional[str], to_unit: Optional[str]) -> Optional[float]:
    if qty is None or from_unit is None or to_unit is None:
        return None
    if from_unit == to_unit:
        return float(qty)
    factor = _UNIT_CONVERSIONS.get((from_unit, to_unit))
    if factor is None:
        return None
    return float(qty) * factor


def calculate_duty(req: DutyRequest) -> DutyResult:
    if req.import_destination == "XI":
        raise NotImplementedError(
            "Northern Ireland (XI) destination is not implemented in this POC. "
            "Real OTT NI flow needs trader scheme (UKIMS), planned processing, "
            "Meursing additional codes."
        )

    flat = re.sub(r"\D", "", req.commodity_code or "").ljust(10, "0")[:10]

    # 0. Money math only makes sense on a declarable leaf (CDS rejects parents).
    if not local_db.is_declarable_leaf(req.commodity_code):
        children = local_db.declarable_leaf_children(req.commodity_code)
        com = local_db.commodity(req.commodity_code)
        return DutyResult(
            commodity_code=req.commodity_code,
            commodity_description=(com or {}).get("description", ""),
            country_of_origin=req.country_of_origin,
            country_name=req.country_of_origin,
            import_destination=req.import_destination,
            import_date=req.import_date,
            customs_value_gbp=req.customs_value_gbp,
            rate_applied=0.0,
            rate_source="n/a",
            customs_duty_gbp=0.0,
            vat_rate=req.vat_rate if req.vat_rate is not None else 20.0,
            notes=["No totals were calculated: this code is not a declarable leaf."],
            needs_more_detail={
                "message": (
                    "This code needs one more level of detail before duty can be "
                    "calculated. Pick the declarable commodity that matches your goods."
                    if children else
                    "This code is not a declarable UK commodity (it may have expired or "
                    "sit at a non-declarable level). Re-run classification to pick a live 10-digit code."
                ),
                "children": children,
            },
        )

    # 1. Pull real applicable measures from the local DB.
    ott_data = local_db.best_applicable_duty(
        req.commodity_code,
        req.country_of_origin,
        as_of=req.import_date,
    )
    notes: list[str] = []
    warnings: list[dict] = []
    eligible: list[dict] = []
    rate_kind = "ad_valorem"
    rate_per_unit: Optional[str] = None
    rate_monetary_unit: Optional[str] = None
    rate_expression = ""
    rate_applied = 0.0
    customs_duty = 0.0
    source = "MFN"
    reqs = commodity_requirements(req.commodity_code)

    # GBP 135 low-value regime is behaviour, not a footnote. Excise goods
    # (alcohol/tobacco/fuel) are excluded from the regime.
    excise_goods = reqs.needs_excise or _excise_gap_warning(flat) is not None
    low_value = req.customs_value_gbp <= 135.0 and not excise_goods

    def _apply_rate(rate_obj: dict) -> float:
        nonlocal rate_kind, rate_per_unit, rate_monetary_unit, rate_expression
        rate_kind = rate_obj["kind"]
        rate_expression = rate_obj.get("text", "")
        if rate_obj["kind"] in ("ad_valorem", "free"):
            pct = float(rate_obj.get("rate_pct") or 0)
            return round(req.customs_value_gbp * pct / 100.0, 2)
        rate_per_unit = rate_obj.get("per_unit")
        rate_monetary_unit = rate_obj.get("monetary_unit")
        amt = float(rate_obj.get("amount") or 0)
        qty_in_unit = _convert_quantity(
            req.quantity_units, req.quantity_unit_type, rate_obj.get("per_unit")
        )
        if qty_in_unit is None:
            supplied = (
                f"{req.quantity_units} {req.quantity_unit_type}"
                if req.quantity_units is not None or req.quantity_unit_type
                else "no quantity"
            )
            raise ValueError(
                f"Specific duty {amt} {rate_monetary_unit}/{rate_per_unit} requires a quantity "
                f"convertible to {rate_per_unit}; trader supplied {supplied}."
            )
        return round(amt * qty_in_unit, 2)

    def _compute_amount(rate_obj: dict) -> Optional[float]:
        """Duty amount for a rate WITHOUT touching the headline rate fields.

        None when a specific rate cannot be evaluated from the supplied
        quantity - callers warn instead of raising (educational stance).
        """
        if rate_obj["kind"] in ("ad_valorem", "free"):
            pct = float(rate_obj.get("rate_pct") or 0)
            return round(req.customs_value_gbp * pct / 100.0, 2)
        qty_in_unit = _convert_quantity(
            req.quantity_units, req.quantity_unit_type, rate_obj.get("per_unit")
        )
        if qty_in_unit is None:
            return None
        return round(float(rate_obj.get("amount") or 0) * qty_in_unit, 2)

    mfn = ott_data.get("mfn")
    pref = ott_data.get("preference")

    if pref:
        eligible.append({
            "group": pref["geographical_area_description"] or pref["geographical_area_id"],
            "rate": pref.get("rate_pct") if pref["kind"] == "ad_valorem" else pref.get("amount"),
            "rate_kind": pref["kind"],
            "rate_expression": pref.get("text", ""),
            "measure_id": pref.get("measure_id"),
            "source": "local_db",
        })

    if pref and req.has_proof_of_origin:
        customs_duty = _apply_rate(pref)
        rate_applied = float(pref.get("rate_pct") or pref.get("amount") or 0)
        source = pref["geographical_area_description"] or pref["geographical_area_id"]
        mfn_text = (mfn or {}).get("text") if mfn else "n/a"
        notes.append(
            f"Preference applied: {source} at {pref.get('text')}. MFN would be {mfn_text}. "
            f"You must hold a valid proof of origin - HMRC can ask to see it. Measure {pref.get('measure_id')}."
        )
    elif pref and not req.has_proof_of_origin and mfn:
        customs_duty = _apply_rate(mfn)
        rate_applied = float(mfn.get("rate_pct") or mfn.get("amount") or 0)
        source = "MFN (Third country duty)"
        notes.append(
            f"Preference '{pref['geographical_area_description']}' at {pref.get('text')} is available but no proof of origin supplied. "
            f"Falling back to MFN rate of {mfn.get('text')}."
        )
    elif mfn:
        customs_duty = _apply_rate(mfn)
        rate_applied = float(mfn.get("rate_pct") or mfn.get("amount") or 0)
        source = "MFN (Third country duty)"
        notes.append(
            f"No UK preference applies for {req.country_of_origin} on this commodity. "
            f"Third country duty (MFN) of {mfn.get('text')} applied. Data from local tariff_db."
        )
    else:
        notes.append("No MFN or preference measures found for this commodity/country combination.")

    all_measures = ott_data.get("all_measures", [])

    # 1b. Suspensions (112/115) and tariff quotas (122/143) can lower the bill.
    if not low_value:
        best_relief: Optional[tuple[float, dict, dict]] = None  # (amount, measure, rate)
        unevaluated_quotas: list[tuple[dict, dict]] = []
        for m in all_measures:
            mt = m["measure_type_id"]
            if mt not in _RELIEF_TYPES:
                continue
            if mt == "143" and not req.has_proof_of_origin:
                continue
            rate = local_db.component_rate(m)
            if rate is None:
                continue
            amount = _compute_amount(rate)
            if amount is None:
                if mt in _QUOTA_TYPES:
                    unevaluated_quotas.append((m, rate))
                continue
            if amount < customs_duty and (best_relief is None or amount < best_relief[0]):
                best_relief = (amount, m, rate)
        if best_relief is not None:
            _, m, rate = best_relief
            prev_text = rate_expression or "the standard rate"
            customs_duty = _apply_rate(rate)
            rate_applied = float(rate.get("rate_pct") or rate.get("amount") or 0)
            mt = m["measure_type_id"]
            label = m["measure_type_description"] or (
                "Tariff quota" if mt in _QUOTA_TYPES else "Tariff suspension"
            )
            source = label
            if mt in _QUOTA_TYPES:
                ordernumber = m.get("ordernumber") or "n/a"
                warnings.append(_warn(
                    "quota",
                    f"Tariff quota {ordernumber} rate of {rate.get('text')} used instead of {prev_text}. "
                    "Quota rates are subject to the remaining quota balance - verify on GOV.UK before relying on this.",
                    rate.get("text"),
                ))
            else:
                extra = (
                    " You must hold an authorised-use authorisation to use this rate."
                    if mt == "115" else ""
                )
                warnings.append(_warn(
                    "suspension_applied",
                    f"{label} at {rate.get('text')} applied instead of {prev_text} - "
                    f"it is lower than the standard rate.{extra}",
                ))
            notes.append(f"{label} (measure {m['measure_sid']}) applied at {rate.get('text')}.")
        for m, rate in unevaluated_quotas:
            ordernumber = m.get("ordernumber") or "n/a"
            warnings.append(_warn(
                "quota",
                f"Tariff quota {ordernumber} at {rate.get('text')} may apply but needs a quantity in "
                f"{rate.get('per_unit')} to evaluate - not used. Subject to quota balance - verify on GOV.UK.",
                rate.get("text"),
            ))

    # 1c. Trade remedies (anti-dumping / countervailing / safeguard) on top.
    if not low_value:
        for label, type_ids in _REMEDY_FAMILIES:
            fam = [m for m in all_measures if m["measure_type_id"] in type_ids]
            rated = [(m, r) for m in fam if (r := local_db.component_rate(m)) is not None]
            if not rated:
                continue
            texts = sorted({r["text"] for _, r in rated})
            has_company_codes = any(m.get("additional_code") for m, _ in rated)
            if len(texts) == 1:
                m0, r0 = rated[0]
                amount = _compute_amount(r0)
                if amount is not None:
                    customs_duty = round(customs_duty + amount, 2)
                    suffix = (
                        " Verify your supplier's additional code - company-specific rates can differ."
                        if has_company_codes else ""
                    )
                    warnings.append(_warn(
                        "trade_remedy",
                        f"{label} of {r0['text']} added on top of the customs duty "
                        f"(GBP {amount:.2f}, measure {m0['measure_sid']}).{suffix}",
                        r0["text"],
                    ))
                    notes.append(f"{label} applied: {r0['text']} (GBP {amount:.2f}).")
                else:
                    warnings.append(_warn(
                        "trade_remedy",
                        f"{label} of {r0['text']} applies to imports from {req.country_of_origin} but needs "
                        f"a quantity in {r0.get('per_unit')} to compute - NOT added to the total.",
                        r0["text"],
                    ))
            else:
                rate_range = _rate_range_text([r for _, r in rated])
                warnings.append(_warn(
                    "trade_remedy",
                    f"{label} between {rate_range} applies to imports from {req.country_of_origin} depending "
                    "on the supplier-specific additional code. NOT added to the total - check your supplier's "
                    "additional code before importing.",
                    rate_range,
                ))
        if any(m["measure_type_id"] == _REMEDY_PENDING_TYPE for m in all_measures):
            warnings.append(_warn(
                "trade_remedy",
                "An anti-dumping/countervailing duty is pending collection on this commodity - "
                "the final liability can be applied retrospectively. Check the measure on GOV.UK.",
            ))

    # 1d. Prohibitions / restrictions - annotate prominently, never block.
    seen_prohibitions: set[tuple] = set()
    for m in all_measures:
        mt = m["measure_type_id"]
        if mt not in _PROHIBITION_TYPES and mt not in _RESTRICTION_TYPES:
            continue
        desc = m["measure_type_description"] or (
            "Import prohibition" if mt in _PROHIBITION_TYPES else "Import restriction"
        )
        geo = m["geographical_area_description"] or m["geographical_area_id"]
        # Types 465/475 share a description - dedupe on the trader-visible text.
        key = (desc, m["geographical_area_id"], mt in _PROHIBITION_TYPES)
        if key in seen_prohibitions:
            continue
        seen_prohibitions.add(key)
        geo_id = str(m.get("geographical_area_id") or "")
        erga_omnes = geo_id == "1011" or "erga omnes" in str(geo).lower()
        if mt in _PROHIBITION_TYPES:
            kind = "prohibition"
            message = (
                f"{desc} ({geo}): these goods may not be importable as described - "
                "the totals below assume the import is permitted."
                " Check GOV.UK sanctions and import-controls guidance: https://www.gov.uk/guidance/uk-sanctions"
            )
        elif erga_omnes:
            # Routine all-origins control notice (attached to huge swathes of the
            # tariff) - informational, not an alarm about this trader's import.
            kind = "other"
            message = (
                f"{desc}: routine import controls can apply to this commodity. This notice "
                "applies to imports from anywhere, not specifically your origin - a licence "
                "or certificate may be needed in some cases."
            )
        else:
            kind = "prohibition"
            message = (
                f"{desc} ({geo}): a licence, certificate or other condition may be "
                "required before import."
                " Check GOV.UK sanctions and import-controls guidance: https://www.gov.uk/guidance/uk-sanctions"
            )
        warnings.append(_warn(kind, message))

    # 1e. GBP 135 low-value regime: no customs duty; VAT becomes supply VAT.
    if low_value:
        if customs_duty > 0:
            notes.append(
                f"GBP 135 low-value regime: no customs duty is charged on consignments valued at or "
                f"below GBP 135 (would have been GBP {customs_duty:.2f})."
            )
        else:
            notes.append(
                "GBP 135 low-value regime: no customs duty is charged on consignments valued at or below GBP 135."
            )
        customs_duty = 0.0
        rate_applied = 0.0
        source = "GBP 135 low-value regime (no customs duty)"
        warnings.append(_warn(
            "other",
            "Consignment value is GBP 135 or less: VAT is supply VAT, charged by the overseas seller "
            "at the point of sale (or accounted for by a VAT-registered buyer via reverse charge), "
            "not import VAT at the border. Excise goods are excluded from this regime.",
        ))

    # 2. Excise: alcohol (local UK Alcohol Duty Reform 2023 table), tobacco, fuel.
    excise_obj: Optional[ExciseBreakdown] = None
    excise_detail: Optional[dict] = None
    excise_amount = 0.0
    if reqs.needs_excise and reqs.excise_category == "alcohol":
        if req.excise_inputs is None:
            raise ValueError(
                "Excise applies to this commodity; ABV and product volume are required before duty can be calculated."
            )
        if not req.excise_inputs.volume_litres or not req.excise_inputs.abv:
            raise ValueError(
                "Excise inputs must include a positive ABV and product volume in litres."
            )
        else:
            calc = calculate_excise(
                beverage_type=reqs.excise_beverage_type or "wine",
                abv=float(req.excise_inputs.abv),
                volume_litres=float(req.excise_inputs.volume_litres),
                is_small_producer=req.excise_inputs.is_small_producer,
                is_draught=req.excise_inputs.is_draught,
            )
            excise_obj = ExciseBreakdown(**calc)
            excise_amount = calc["duty_gbp"]
    elif reqs.needs_excise and reqs.excise_category == "tobacco":
        excise_amount, excise_detail = _calculate_tobacco_excise(flat, req, warnings)
    elif reqs.needs_excise and reqs.excise_category == "fuel":
        excise_amount, excise_detail = _calculate_fuel_excise(flat, req, warnings)
    gap_warning = _excise_gap_warning(flat)
    if gap_warning:
        warnings.append(gap_warning)

    # 3. Meursing/additional-code composition capture.
    meursing_obj: Optional[dict] = None
    if reqs.needs_meursing_code:
        if req.meursing_inputs is None:
            notes.append(
                "This processed-food code may need Meursing/additional-code composition facts "
                "for NI/EU declaration handoff; no composition inputs were supplied."
            )
        else:
            meursing_obj = resolve_meursing(req.meursing_inputs)
            code = meursing_obj.get("additional_code")
            if code:
                notes.append(
                    f"Meursing/additional code {code} resolved from the supplied composition bands. "
                    "This is carried into the declaration handoff in the local demo."
                )
            else:
                notes.append(
                    "Meursing/additional-code composition facts were captured, but this local demo "
                    "does not have a deterministic code mapping for that exact band path. Verify in GOV.UK lookup."
                )

    # Only None means "unset" - an explicit 0 is a real zero VAT rate.
    if req.vat_rate is not None:
        vat_rate = req.vat_rate
    else:
        db_vat = ott_data.get("vat_rate")
        vat_rate = float(db_vat) if db_vat is not None else 20.0

    return DutyResult(
        commodity_code=req.commodity_code,
        commodity_description=ott_data.get("description", ""),
        country_of_origin=req.country_of_origin,
        country_name=req.country_of_origin,
        import_destination=req.import_destination,
        import_date=req.import_date,
        customs_value_gbp=req.customs_value_gbp,
        rate_applied=rate_applied,
        rate_kind=rate_kind,
        rate_per_unit=rate_per_unit,
        rate_monetary_unit=rate_monetary_unit,
        rate_expression=rate_expression,
        rate_source=source,
        eligible_preferences=eligible,
        customs_duty_gbp=customs_duty,
        excise=excise_obj,
        excise_duty_gbp=excise_amount,
        excise_detail=excise_detail,
        meursing=meursing_obj,
        vat_rate=vat_rate,
        notes=notes,
        warnings=warnings,
        low_value_regime=low_value,
        measures_inspected=[
            {
                "measure_id": str(m["measure_sid"]),
                "measure_type_id": m["measure_type_id"],
                "measure_type_description": m["measure_type_description"],
                "geographical_area": m["geographical_area_description"] or m["geographical_area_id"],
                "duty_expression": _format_components(m.get("components") or []),
                "additional_code": m.get("additional_code"),
                "ordernumber": m.get("ordernumber"),
            }
            for m in ott_data.get("all_measures", [])
        ],
    )


def _rate_range_text(rates: list[dict]) -> str:
    """Compact range for ambiguous (additional-code-dependent) remedy rates."""
    if all(r["kind"] in ("ad_valorem", "free") for r in rates):
        pcts = [float(r.get("rate_pct") or 0) for r in rates]
        return f"{min(pcts):g}% - {max(pcts):g}%"
    return " / ".join(sorted({r["text"] for r in rates})[:4])


def _format_components(components: list[dict]) -> str:
    if not components:
        return ""
    c = components[0]
    amt = c.get("duty_amount")
    if amt is None:
        return ""
    if c.get("monetary_unit_code") and c.get("measurement_unit_code"):
        return f"{amt} {c['monetary_unit_code']} / {c['measurement_unit_code']}"
    return f"{amt:.2f} %"
