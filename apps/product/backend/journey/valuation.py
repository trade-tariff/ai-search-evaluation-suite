"""Customs valuation helpers for the e2e trader journey.

The journey supports the WTO/HMRC valuation method order:

1. transaction value;
2. transaction value of identical goods;
3. transaction value of similar goods;
4. deductive value;
5. computed value;
6. fallback value.

The calculators are deliberately deterministic. The guided UI chooses the first
method for which the trader says they have usable evidence, then this module
does the arithmetic and returns an auditable breakdown.
"""
from __future__ import annotations

from .schemas import (
    ValuationGuideRequest,
    ValuationGuideResult,
    ValuationMethodChoice,
    ValuationRequest,
    ValuationResult,
)

METHOD_LABELS = {
    "method_1_transaction_value": "Method 1 (transaction value)",
    "method_2_identical_goods": "Method 2 (transaction value of identical goods)",
    "method_3_similar_goods": "Method 3 (transaction value of similar goods)",
    "method_4_deductive": "Method 4 (deductive value)",
    "method_5_computed": "Method 5 (computed value)",
    "method_6_fallback": "Method 6 (fallback value)",
    "known_customs_value": "Trader-provided customs value",
}


def calculate_customs_value(req: ValuationRequest) -> ValuationResult:
    if req.known_customs_value_gbp is not None:
        value = round(float(req.known_customs_value_gbp), 2)
        return ValuationResult(
            customs_value_gbp=value,
            breakdown={"customs_value_gbp": value},
            method=METHOD_LABELS["known_customs_value"],
            method_code="known_customs_value",
            confidence="known",
            notes=["Trader stated they already know the customs value. No valuation-method arithmetic was applied."],
        )

    method = req.method or "method_1_transaction_value"
    if method == "method_1_transaction_value":
        return _method_1(req)
    return calculate_method_value(method, req.method_inputs)


def choose_valuation_method(req: ValuationGuideRequest) -> ValuationGuideResult:
    if req.has_sale_for_export and req.has_usable_transaction_value:
        choice = _choice("method_1_transaction_value", "There is a sale for export and a usable price actually paid or payable.")
    elif req.has_identical_goods_value:
        choice = _choice("method_2_identical_goods", "Method 1 is not available, but accepted values for identical goods are available.")
    elif req.has_similar_goods_value:
        choice = _choice("method_3_similar_goods", "No identical-goods value is available, but accepted values for similar goods are available.")
    elif req.try_computed_before_deductive and req.has_production_costs:
        choice = _choice("method_5_computed", "The importer elected to try Method 5 before Method 4 and has producer-side cost evidence.")
    elif req.has_uk_resale_price:
        choice = _choice("method_4_deductive", "No transaction comparator is available, but there is a UK resale price to deduct from.")
    elif req.has_production_costs:
        choice = _choice("method_5_computed", "Production cost, profit, packing, and transport evidence is available.")
    else:
        choice = _choice("method_6_fallback", "Earlier methods are unavailable, so use a reasonable fallback based on available evidence.")

    required = _required_inputs(choice.method_code)
    result = None
    notes = ["Valuation methods must be considered in order; do not jump to a later method if an earlier method is usable."]
    if _has_required_inputs(req.inputs, required):
        result = calculate_method_value(choice.method_code, req.inputs)
    else:
        missing = [k for k in required if _to_float(req.inputs.get(k)) is None]
        notes.append("Missing numeric inputs: " + ", ".join(missing))
    return ValuationGuideResult(choice=choice, result=result, required_inputs=required, notes=notes)


def calculate_method_value(method_code: str, inputs: dict) -> ValuationResult:
    if method_code == "method_1_transaction_value":
        req = ValuationRequest(
            invoice_value=_num(inputs, "invoice_value"),
            invoice_currency=str(inputs.get("invoice_currency") or "GBP"),
            fx_rate_to_gbp=_num(inputs, "fx_rate_to_gbp", 1.0),
            freight_gbp=_num(inputs, "freight_gbp"),
            insurance_gbp=_num(inputs, "insurance_gbp"),
            other_costs_gbp=_num(inputs, "other_costs_gbp"),
        )
        return _method_1(req)

    if method_code in ("method_2_identical_goods", "method_3_similar_goods"):
        base_key = "accepted_identical_value_gbp" if method_code == "method_2_identical_goods" else "accepted_similar_value_gbp"
        base = _num(inputs, base_key)
        quantity_adjustment = _num(inputs, "quantity_adjustment_gbp")
        commercial_level_adjustment = _num(inputs, "commercial_level_adjustment_gbp")
        transport_adjustment = _num(inputs, "transport_adjustment_gbp")
        value = round(base + quantity_adjustment + commercial_level_adjustment + transport_adjustment, 2)
        return ValuationResult(
            customs_value_gbp=value,
            breakdown={
                base_key: base,
                "quantity_adjustment_gbp": quantity_adjustment,
                "commercial_level_adjustment_gbp": commercial_level_adjustment,
                "transport_adjustment_gbp": transport_adjustment,
                "customs_value_gbp": value,
            },
            method=METHOD_LABELS[method_code],
            method_code=method_code,
            confidence="estimate",
            notes=["Comparator values should be accepted customs values for goods exported at or about the same time."],
        )

    if method_code == "method_4_deductive":
        resale = _num(inputs, "uk_resale_price_gbp")
        commissions = _num(inputs, "commissions_or_profit_gbp")
        uk_transport = _num(inputs, "uk_transport_gbp")
        uk_duties_taxes = _num(inputs, "uk_duties_taxes_gbp")
        value = round(max(0.0, resale - commissions - uk_transport - uk_duties_taxes), 2)
        return ValuationResult(
            customs_value_gbp=value,
            breakdown={
                "uk_resale_price_gbp": resale,
                "less_commissions_or_profit_gbp": commissions,
                "less_uk_transport_gbp": uk_transport,
                "less_uk_duties_taxes_gbp": uk_duties_taxes,
                "customs_value_gbp": value,
            },
            method=METHOD_LABELS[method_code],
            method_code=method_code,
            confidence="estimate",
            notes=["Deductive value starts from the UK resale price and deducts post-import costs and taxes."],
        )

    if method_code == "method_5_computed":
        materials = _num(inputs, "materials_gbp")
        manufacturing = _num(inputs, "manufacturing_gbp")
        profit = _num(inputs, "producer_profit_gbp")
        packing = _num(inputs, "packing_gbp")
        transport = _num(inputs, "transport_to_import_gbp")
        value = round(materials + manufacturing + profit + packing + transport, 2)
        return ValuationResult(
            customs_value_gbp=value,
            breakdown={
                "materials_gbp": materials,
                "manufacturing_gbp": manufacturing,
                "producer_profit_gbp": profit,
                "packing_gbp": packing,
                "transport_to_import_gbp": transport,
                "customs_value_gbp": value,
            },
            method=METHOD_LABELS[method_code],
            method_code=method_code,
            confidence="estimate",
            notes=["Computed value uses producer-side cost and profit evidence; traders rarely have enough evidence for this method."],
        )

    base = _num(inputs, "reasonable_base_value_gbp")
    adjustments = _num(inputs, "adjustments_gbp")
    value = round(max(0.0, base + adjustments), 2)
    return ValuationResult(
        customs_value_gbp=value,
        breakdown={
            "reasonable_base_value_gbp": base,
            "adjustments_gbp": adjustments,
            "customs_value_gbp": value,
        },
        method=METHOD_LABELS["method_6_fallback"],
        method_code="method_6_fallback",
        confidence="estimate",
        notes=["Fallback value must use reasonable means consistent with the earlier methods; arbitrary or prohibited bases are not acceptable."],
    )


def _method_1(req: ValuationRequest) -> ValuationResult:
    invoice_gbp = round(req.invoice_value * req.fx_rate_to_gbp, 2)
    customs_value = round(
        invoice_gbp + req.freight_gbp + req.insurance_gbp + req.other_costs_gbp, 2
    )
    breakdown = {
        "invoice_in_gbp": invoice_gbp,
        "freight_gbp": req.freight_gbp,
        "insurance_gbp": req.insurance_gbp,
        "other_costs_gbp": req.other_costs_gbp,
        "customs_value_gbp": customs_value,
    }
    notes = []
    if req.invoice_currency != "GBP":
        notes.append(
            f"Invoice converted from {req.invoice_currency} at {req.fx_rate_to_gbp} GBP per {req.invoice_currency}"
        )
    if customs_value > 135 and customs_value <= 800:
        notes.append("Low-value consignment threshold (GBP 135) exceeded. Standard import declaration required.")
    if customs_value <= 135:
        notes.append("Consignment value at or below GBP 135: simplified low-value rules may apply.")
    return ValuationResult(
        customs_value_gbp=customs_value,
        breakdown=breakdown,
        method=METHOD_LABELS["method_1_transaction_value"],
        method_code="method_1_transaction_value",
        confidence="calculated",
        notes=notes,
    )


def _choice(method_code: str, reason: str) -> ValuationMethodChoice:
    return ValuationMethodChoice(method_code=method_code, method=METHOD_LABELS[method_code], reason=reason)


def _required_inputs(method_code: str) -> list[str]:
    return {
        "method_1_transaction_value": ["invoice_value"],
        "method_2_identical_goods": ["accepted_identical_value_gbp"],
        "method_3_similar_goods": ["accepted_similar_value_gbp"],
        "method_4_deductive": ["uk_resale_price_gbp", "commissions_or_profit_gbp", "uk_transport_gbp", "uk_duties_taxes_gbp"],
        "method_5_computed": ["materials_gbp", "manufacturing_gbp", "producer_profit_gbp", "packing_gbp", "transport_to_import_gbp"],
        "method_6_fallback": ["reasonable_base_value_gbp"],
    }[method_code]


def _has_required_inputs(inputs: dict, required: list[str]) -> bool:
    return all(_to_float(inputs.get(k)) is not None for k in required)


def _to_float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _num(inputs: dict, key: str, default: float = 0.0) -> float:
    value = _to_float(inputs.get(key))
    return default if value is None else value
