"""Landed cost = customs value + duty + excise + VAT + any extras.

VAT in the UK is charged on customs value + duty (and excise where it applies)
PLUS incidental costs up to the first UK destination (port handling, onward
freight) - those go in `incidental_costs_to_uk_gbp`. Post-arrival charges
(`additional_charges_gbp`) stay outside the VAT base. Standard rate 20%. Some
categories (e.g. children's clothing, books) are 0% but we keep the rate
caller-driven for the POC.

GBP 135 consignments: no customs duty and VAT is supply VAT (seller charges at
point of sale) rather than import VAT. Postponed VAT accounting (PVA) moves
import VAT off the border bill and onto the VAT return.
"""
from __future__ import annotations

from .schemas import LandedRequest, LandedResult


def calculate_landed(req: LandedRequest) -> LandedResult:
    low_value = (
        req.low_value_import
        if req.low_value_import is not None
        else (req.customs_value_gbp <= 135.0 and req.excise_duty_gbp == 0)
    )
    vat_base = round(
        req.customs_value_gbp
        + req.customs_duty_gbp
        + req.excise_duty_gbp
        + req.incidental_costs_to_uk_gbp,
        2,
    )
    vat = round(vat_base * req.vat_rate / 100.0, 2)
    if low_value:
        vat_treatment = "supply_vat"
    elif req.use_postponed_vat:
        vat_treatment = "postponed"
    else:
        vat_treatment = "import_vat"
    duties = round(req.customs_duty_gbp + req.excise_duty_gbp, 2)
    cash_at_border = round(duties + (vat if vat_treatment == "import_vat" else 0.0), 2)
    total_tax_liability = round(duties + vat, 2)
    total = round(
        req.customs_value_gbp
        + req.customs_duty_gbp
        + req.excise_duty_gbp
        + req.incidental_costs_to_uk_gbp
        + vat
        + req.additional_charges_gbp,
        2,
    )
    breakdown = {
        "customs_value_gbp": req.customs_value_gbp,
        "customs_duty_gbp": req.customs_duty_gbp,
        "excise_duty_gbp": req.excise_duty_gbp,
        "incidental_costs_to_uk_gbp": req.incidental_costs_to_uk_gbp,
        "vat_taxable_amount_gbp": vat_base,
        "vat_gbp": vat,
        "additional_charges_gbp": req.additional_charges_gbp,
        "cash_at_border_gbp": cash_at_border,
        "total_tax_liability_gbp": total_tax_liability,
        "total_landed_cost_gbp": total,
    }
    notes = [
        f"VAT computed on customs value + customs duty + excise duty at {req.vat_rate}%.",
    ]
    if req.incidental_costs_to_uk_gbp > 0:
        notes[0] = (
            f"VAT computed on customs value + customs duty + excise duty + incidental costs "
            f"to the first UK destination at {req.vat_rate}%."
        )
    if low_value:
        notes.insert(0, (
            "GBP 135 low-value regime: no customs duty is due and the VAT shown is supply VAT - "
            "the overseas seller charges UK VAT at the point of sale (or a VAT-registered buyer "
            "accounts for it via reverse charge). It is NOT import VAT collected at the border."
        ))
    elif vat_treatment == "postponed":
        notes.append(
            "Postponed VAT accounting: import VAT is accounted for on your VAT return "
            "(no cash at the border). Cash at border covers customs and excise duty only."
        )
    if req.additional_charges_gbp > 0:
        notes.append(
            f"Additional charges of £{req.additional_charges_gbp} added after VAT (e.g. broker, "
            "post-arrival UK haulage) - outside the import VAT base."
        )
    return LandedResult(
        vat_taxable_amount_gbp=vat_base,
        vat_gbp=vat,
        total_landed_cost_gbp=total,
        breakdown=breakdown,
        notes=notes,
        vat_treatment=vat_treatment,
        cash_at_border_gbp=cash_at_border,
        total_tax_liability_gbp=total_tax_liability,
    )
