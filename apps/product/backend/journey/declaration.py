"""CDS-style declaration draft.

Produces a draft with the data elements traders typically need to provide on a
CHIEF/CDS H1 (import) declaration. Real CDS uses Data Elements (DE 1/1, 1/2,
etc.) - we map our journey state to those box references for educational value.

Not a submission. No XML. No EORI validation. POC only.
"""
from __future__ import annotations

import re

from . import local_db
from .schemas import DeclarationRequest, DeclarationResult


# Certs with long real-world lead times. We highlight these so a trader isn't
# blindsided. Mapping is partial - kept narrow to what shows up in the slice.
SLOW_CERT_PREFIXES = {
    "L001": ("CITES import permit", "2-6 weeks lead time, apply via APHA"),
    "C400": ("CITES certificate (Washington Convention)", "Apply to APHA (UK Management Authority); 2-6 weeks lead time"),
    "C640": ("CHED-A (live animal entry)", "Pre-notify in IPAFFS; BCP inspection required"),
    "C641": ("CHED-PP (plant health)", "Pre-notify in IPAFFS; phytosanitary cert required from exporter"),
    "C673": ("Catch certificate (IUU)", "Pre-notify and obtain from flag state authority"),
    "N002": ("Veterinary health certificate", "Issued by official vet in country of origin"),
    "N853": ("Organic certificate (TRACES NT)", "Issued by control body in country of origin"),
    "C644": ("Organic import certificate (COI)", "Issued by TRACES NT, control body validated"),
    "Y930": ("End-use authorisation", "Apply to HMRC; check eligibility"),
}


PREFERENCE_TO_DOCUMENT_CODE = {
    "EU_TCA": [
        {"code": "U116", "description": "Statement on Origin (EU-UK Trade and Cooperation Agreement)"},
    ],
    "JP_FTA": [
        {"code": "U110", "description": "Statement on Origin (UK-Japan CEPA)"},
    ],
    "AU_FTA": [
        {"code": "U118", "description": "Origin certification (UK-Australia FTA)"},
    ],
    "NZ_FTA": [
        {"code": "U119", "description": "Origin certification (UK-New Zealand FTA)"},
    ],
    "CA_FTA": [
        {"code": "U112", "description": "Statement on Origin (UK-Canada Trade Continuity Agreement)"},
    ],
    "CPTPP": [
        {"code": "U117", "description": "Certification of Origin (CPTPP)"},
    ],
    "DCTS_STD": [
        {"code": "N865", "description": "GSP Form A or origin declaration (DCTS Standard)"},
    ],
    "DCTS_ENH": [
        {"code": "N865", "description": "GSP Form A or origin declaration (DCTS Enhanced)"},
    ],
    "DCTS_LDC": [
        {"code": "N865", "description": "GSP Form A or origin declaration (DCTS LDC)"},
    ],
}

VALUATION_METHOD_LABELS = {
    "known_customs_value": "Trader-provided customs value",
    "method_1_transaction_value": "1 (transaction value)",
    "method_2_identical_goods": "2 (transaction value of identical goods)",
    "method_3_similar_goods": "3 (transaction value of similar goods)",
    "method_4_deductive": "4 (deductive value)",
    "method_5_computed": "5 (computed value)",
    "method_6_fallback": "6 (fallback value)",
}


def _cert_documents_live(code: str, country: str | None, import_date: str | None = None) -> list[dict]:
    """Resolve cert documents from the live scenario (code + country chain +
    inheritance) instead of the KG facets snapshot.

    Per codex's review: "Declaration reads should be live. Classification can
    show likely documents with caveats, but the declaration draft must resolve
    documents after code, country, date, destination and preference are known."
    """
    if not code:
        return []
    country = country or "1011"  # ERGA OMNES default
    try:
        flat = re.sub(r"\D", "", code or "")
        requirements = local_db.import_requirements(flat, country, as_of=import_date)
        return requirements.get("cert_documents") or []
    except Exception as exc:
        print(f"[declaration] live cert lookup failed for {code}/{country}: {exc}")
        return []


def build_declaration(req: DeclarationRequest) -> DeclarationResult:
    # CDS uses procedure code 4000 for "release for free circulation, with no
    # previous procedure" - the default for a straightforward import.
    procedure_code = "4000"
    additional_procedure_code = "000" if not req.preference_claimed else "300"

    pref = req.preference_claimed if req.preference_claimed and req.preference_claimed != "MFN" else None
    additional_codes = _normalise_additional_codes(req.additional_codes)
    code_parts = _split_commodity_code(req.commodity_code)

    documents = []
    slow_cert_warnings: list[str] = []
    if pref:
        documents.extend(PREFERENCE_TO_DOCUMENT_CODE.get(pref, []))
    if req.excise_gbp > 0:
        documents.append(
            {"code": "Y929", "description": "Excise goods - registration / movement documents required"}
        )
    # Resolve cert documents live for the scenario (code + parents).
    # Codex's review: "Declaration reads should be live, not snapshot-via-facets."
    seen_codes = {d["code"] for d in documents}
    for doc in _cert_documents_live(req.commodity_code, req.country_of_origin, req.import_date):
        cert = doc["code"]
        if cert in seen_codes or not cert:
            continue
        desc = (doc.get("description") or "").strip()
        desc = re.sub(r"<[^>]+>", "", desc).strip()
        # Append the condition meaning for context (e.g. "Condition B: presentation required").
        cond_desc = (doc.get("condition_desc") or "").strip()
        if cond_desc and cond_desc not in desc:
            desc = f"{desc}. Condition {doc.get('condition_code', '?')}: {cond_desc}".strip()
        attached_at = doc.get("attached_at") or ""
        inherited = doc.get("inherited", False)
        documents.append({
            "code": cert,
            "description": desc[:280],
            "source": "measure_condition" + (f" (inherited from {attached_at})" if inherited else ""),
            "attached_at": attached_at,
            "inherited": inherited,
        })
        seen_codes.add(cert)
        slow = SLOW_CERT_PREFIXES.get(cert)
        if slow:
            slow_cert_warnings.append(f"{cert} ({slow[0]}) - {slow[1]}.")

    boxes = {
        "DE 1/1 Declaration type": "IM",
        "DE 1/2 Additional declaration type": "A (standard, electronic)",
        "DE 1/10 Procedure": procedure_code,
        "DE 1/11 Additional Procedure": additional_procedure_code,
        "DE 5/15 Country of origin (preferential)": req.country_of_origin if pref else "",
        "DE 5/16 Country of origin (non-preferential)": req.country_of_origin,
        "Import date used for tariff/document checks": req.import_date or "",
        "DE 6/8 Description of goods": req.description_of_goods,
        "DE 6/14 Commodity code (CN)": code_parts["cn"],
        "DE 6/15 Commodity code (TARIC/additional digits)": code_parts["taric"],
        "DE 6/17 National additional digits": code_parts["national"],
        "DE 6/16 Additional code(s)": ", ".join(
            f"{a['type']} {a['code']}" if a.get("type") else a["code"]
            for a in additional_codes
        ),
        "DE 4/14 Item price / amount (£)": f"{req.customs_value_gbp:.2f}",
        "DE 6/1 Net mass (kg)": f"{req.net_mass_kg:.2f}" if req.net_mass_kg else "",
        "DE 6/2 Supplementary units": (
            f"{req.quantity_units} {req.quantity_unit_type}"
            if req.quantity_units and req.quantity_unit_type
            else ""
        ),
        "DE 4/16 Valuation method": VALUATION_METHOD_LABELS.get(req.valuation_method or "", req.valuation_method or ""),
        "DE 4/17 Preference": "300" if pref else "100",
        "Computed customs duty (£)": f"{req.duty_gbp:.2f}",
        "Computed excise duty (£)": f"{req.excise_gbp:.2f}",
        "Computed VAT (£)": f"{req.vat_gbp:.2f}",
    }

    summary = {
        "customs_value_gbp": req.customs_value_gbp,
        "duty_gbp": req.duty_gbp,
        "excise_gbp": req.excise_gbp,
        "vat_gbp": req.vat_gbp,
        "total_taxes_gbp": round(req.duty_gbp + req.excise_gbp + req.vat_gbp, 2),
    }

    next_steps = [
        "Register for an EORI number starting with GB (https://www.gov.uk/eori) if you don't have one.",
        "Open a Customs Declaration Service (CDS) account or have a customs broker submit on your behalf.",
        "Arrange a Duty Deferment Account, Cash Account, or upfront payment for the duty and VAT shown.",
        "Have the supporting documents on hand: invoice, packing list, transport document"
        + (", proof of origin" if pref else "")
        + (", excise documents" if req.excise_gbp > 0 else "")
        + ".",
        "Lodge the declaration ahead of the goods arriving. Once accepted, expect a Movement Reference Number (MRN).",
    ]
    # Surface slow-lead-time certs prominently so the trader doesn't start the
    # journey and discover at the BCP that they're missing a CITES permit.
    if slow_cert_warnings:
        next_steps.insert(
            0,
            "Heads up - this commodity may need certificates with long lead times: "
            + " ".join(slow_cert_warnings),
        )
    if additional_codes:
        next_steps.insert(
            0,
            "Confirm whether the additional code is required for this exact declaration route "
            "(for example Northern Ireland/EU-style Meursing handoff) before filing.",
        )

    # Audit summary - the final breadcrumb trail of how we got here.
    # Codex's review: "Generate a final audit summary: query, Q&A, chosen code,
    # alternatives rejected, duty measures, document measures and source IDs."
    audit_summary = _build_audit_summary(req, documents)

    return DeclarationResult(
        cds_box_values=boxes,
        required_document_codes=documents,
        summary=summary,
        next_steps=next_steps,
        audit_summary=audit_summary,
    )


def _build_audit_summary(req: DeclarationRequest, documents: list[dict]) -> dict:
    """End-of-journey breadcrumb: query, Q&A, chosen code, rejected alternatives,
    duty/document measures, source IDs.
    """
    try:
        reqs = local_db.import_requirements(
            req.commodity_code, req.country_of_origin, req.preference_claimed, as_of=req.import_date,
        )
        measure_ids = [m["measure_sid"] for m in reqs.get("measures", [])]
        duty_measure_ids = [m["measure_sid"] for m in reqs.get("duty_measures", [])]
    except Exception as exc:
        print(f"[declaration audit] measure resolution failed: {exc}")
        measure_ids, duty_measure_ids = [], []

    # Document codes by source ("hardcoded" preference + KG-resolved certs)
    docs_by_source: dict[str, list[str]] = {}
    for d in documents:
        src = d.get("source", "preference")
        docs_by_source.setdefault(src, []).append(d["code"])

    return {
        "original_query": req.original_query,
        "qa_history": req.qa_history or [],
        "chosen_code": req.commodity_code,
        "chosen_description": req.description_of_goods,
        "rejected_candidates": req.rejected_candidates or [],
        "scenario": {
            "country_of_origin": req.country_of_origin,
            "import_date": req.import_date,
            "preference_claimed": req.preference_claimed,
            "customs_value_gbp": req.customs_value_gbp,
            "additional_codes": _normalise_additional_codes(req.additional_codes),
        },
        "measure_ids": measure_ids,
        "duty_measure_ids": duty_measure_ids,
        "document_codes": [d["code"] for d in documents],
        "document_codes_by_source": docs_by_source,
        "computed_totals": {
            "duty_gbp": req.duty_gbp,
            "excise_gbp": req.excise_gbp,
            "vat_gbp": req.vat_gbp,
            "total_taxes_gbp": round(req.duty_gbp + req.excise_gbp + req.vat_gbp, 2),
            "total_landed_gbp": round(
                req.customs_value_gbp + req.duty_gbp + req.excise_gbp + req.vat_gbp, 2,
            ),
        },
    }


def _normalise_additional_codes(additional_codes: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    for raw in additional_codes or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or raw.get("additional_code") or "").strip()
        if not code:
            continue
        out.append({
            "type": str(raw.get("type") or raw.get("code_type") or "Additional code").strip(),
            "code": code,
            "source": str(raw.get("source") or raw.get("lookup_url") or "").strip(),
        })
    return out


def _split_commodity_code(code: str) -> dict[str, str]:
    flat = re.sub(r"\D", "", code or "")
    return {
        "cn": flat[:8],
        "taric": flat[8:10] if len(flat) > 8 else "",
        "national": flat[10:] if len(flat) > 10 else "",
    }
