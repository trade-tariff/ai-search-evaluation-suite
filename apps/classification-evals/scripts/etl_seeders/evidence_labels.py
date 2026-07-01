"""Evidence consumer scopes and semantic roles for the local KG.

Two dimensions are deliberately separate:

- use_scopes: which app consumers may use the row.
- evidence_roles: what kind of evidence the row represents.

That keeps, for example, a Search Reference from looking like a trader-facing
product facet just because it is useful for retrieval.
"""
from __future__ import annotations

import os
import re
from typing import Any


USE_SCOPE_DEFINITIONS: dict[str, tuple[str, str]] = {
    "retrieval": ("Retrieval", "Candidate retrieval and ranking from trader product text."),
    "classification": ("Classification", "Classification reasoning and final candidate selection."),
    "qa": ("Q&A", "Trader-facing classification questions and answer options."),
    "valuation": ("Value", "Customs valuation method selection, inputs, and value calculation."),
    "duty": ("Duty", "Duty, excise, VAT-rate, measure, preference, and additional-code calculation."),
    "landed_cost": ("Landed Cost", "Import-cost presentation: customs value, duty, excise, VAT, charges, and total cost."),
    "declaration": ("Declaration", "Certificates, documents, footnotes, declaration handoff, and compliance data."),
    "audit": ("Audit", "Evidence display, provenance, explanation, and debugging."),
}


EVIDENCE_ROLE_DEFINITIONS: dict[str, tuple[str, str]] = {
    "alias": ("Alias", "Trader/common vocabulary that points to a commodity code."),
    "product_identity": ("Product Identity", "What the goods are, product family, or category."),
    "material_composition": ("Material / Composition", "Material, ingredient, substance, or composition facts."),
    "form_presentation": ("Form / Presentation", "Physical form, processing state, construction, or presentation."),
    "function_use": ("Function / Use", "Intended use, function, end use, or application."),
    "packaging_quantity": ("Packaging / Quantity", "Pack size, container, volume, weight, dimensions, or thresholds."),
    "composition_threshold": ("Composition Threshold", "Threshold values such as alcohol, fat, protein, sugar, starch, or solids."),
    "additional_code": ("Additional Code", "Meursing or other additional-code input or result."),
    "origin_or_region": ("Origin / Region", "Origin, consignment, preference geography, appellation, or region."),
    "legal_definition": ("Legal Definition", "Legal definition or term meaning from GIRs, notes, or HSEN."),
    "legal_inclusion": ("Legal Inclusion", "Rule saying goods are included in a scope."),
    "legal_exclusion": ("Legal Exclusion", "Rule saying goods are excluded from a scope."),
    "classification_order": ("Classification Order", "Ordering rule such as GIR order or chapter/section precedence."),
    "classification_rationale": ("Rationale", "Ruling or extracted rationale for classification."),
    "interpretive_guidance": ("Interpretive Guidance", "HSEN or other interpretive guidance."),
    "heading_guidance": ("Heading Guidance", "Heading-level interpretive guidance."),
    "measure_condition": ("Measure Condition", "Measure condition or operational requirement."),
    "document_requirement": ("Document Requirement", "Certificate, licence, document, or footnote requirement."),
    "duty_rate_measure": ("Duty Rate / Measure", "Duty, VAT-rate, excise, quota, suspension, or preference measure."),
    "valuation_input": ("Valuation Input", "Invoice, freight, insurance, comparator, deductive, computed, or fallback value input."),
    "valuation_method": ("Valuation Method", "One of the customs valuation method rules or choices."),
    "landed_cost_component": ("Landed Cost Component", "Customs value, duty, excise, VAT, broker fees, port charges, or totals used in import-cost presentation."),
    "landed_cost_result": ("Landed Cost Result", "Calculated landed-cost, VAT taxable amount, or total payable result."),
    "declaration_data": ("Declaration Data", "Data carried into declaration/handoff."),
    "footnote": ("Footnote", "Footnote text attached to a commodity or measure."),
    "index_text": ("Index Text", "Search-only text artifact, not itself a fact."),
    "unknown": ("Unknown", "Unclassified evidence role."),
}


DEPLOYABLE_USE_SCOPES = ("retrieval", "classification", "qa", "audit")
DEPLOYABLE_CONSUMER_USE_SCOPES = ("retrieval", "classification", "qa")
DEPLOYABLE_EVIDENCE_ROLES = (
    "alias",
    "product_identity",
    "material_composition",
    "form_presentation",
    "function_use",
    "packaging_quantity",
    "composition_threshold",
    "additional_code",
    "origin_or_region",
    "legal_definition",
    "legal_inclusion",
    "legal_exclusion",
    "classification_order",
    "classification_rationale",
    "interpretive_guidance",
    "heading_guidance",
    "footnote",
    "index_text",
    "unknown",
)


def kg_label_profile() -> str:
    return os.environ.get("AI_FAN_OUT_KG_LABEL_PROFILE", "full").strip().lower() or "full"


def deployable_profile_enabled() -> bool:
    return kg_label_profile() in {"deploy", "deployable", "classification"}


def active_use_scope_definitions() -> dict[str, tuple[str, str]]:
    if not deployable_profile_enabled():
        return USE_SCOPE_DEFINITIONS
    return {key: USE_SCOPE_DEFINITIONS[key] for key in DEPLOYABLE_USE_SCOPES}


def active_evidence_role_definitions() -> dict[str, tuple[str, str]]:
    if not deployable_profile_enabled():
        return EVIDENCE_ROLE_DEFINITIONS
    return {key: EVIDENCE_ROLE_DEFINITIONS[key] for key in DEPLOYABLE_EVIDENCE_ROLES}


def profile_labels(use_scopes: list[str], evidence_roles: list[str]) -> tuple[list[str], list[str]]:
    if not deployable_profile_enabled():
        return _dedupe(use_scopes), _dedupe(evidence_roles)
    scoped = [scope for scope in _dedupe(use_scopes) if scope in DEPLOYABLE_USE_SCOPES]
    roles = [role for role in _dedupe(evidence_roles) if role in DEPLOYABLE_EVIDENCE_ROLES]
    return scoped or ["audit"], roles or ["unknown"]


_EXCLUSION_RE = re.compile(r"(exclude|excluded|excludes|exclusion)")
_TRADE_GEO_RE = re.compile(r"(country_of_origin|country_of_dispatch|destination|geographical_area|geograph.*area|consign|import_origin|preference|quota)")
_PRODUCT_GEO_RE = re.compile(r"(origin|region|appellation|pdo|pgi|designation)")
_DUTY_RE = re.compile(r"(duty|vat|quota|measure|certificate|document|licen[cs]e|relief|suspension|proof)")
_VALUE_RE = re.compile(r"(invoice|freight|insurance|customs_value|value|price|cost|fx|resale|computed|deductive|fallback)")
_ADDITIONAL_CODE_RE = re.compile(r"(meursing|additional_code|starch|sucrose|glucose|milk_fat|milk_protein|milk_solids)")
_COMPOSITION_RE = re.compile(r"(material|composition|ingredient|component|substance|protein|fat|sugar|alcohol|abv|content|carbon)")
_FORM_RE = re.compile(r"(form|state|processing|process|prepared|presentation|construction|manufactur|coating|fermentation)")
_USE_RE = re.compile(r"(function|use|purpose|application|end_use)")
_PACKAGING_RE = re.compile(r"(package|packing|container|net|weight|volume|size|capacity|dimension|diameter|thickness|length|width|cross_section|strength)")
_IDENTITY_RE = re.compile(r"(product|type|category|name|designation|article|beverage|wine|footwear|head_type)")


def _facet_labels_full(source: str, key: str, value: Any | None = None) -> tuple[list[str], list[str]]:
    """Return (use_scopes, evidence_roles) for a commodity_facet row."""
    src = (source or "").lower()
    lowered = (key or "").lower()

    if src == "search_reference" or lowered == "common_term":
        return ["retrieval", "audit"], ["alias"]

    if src == "measure_condition" or lowered == "requires_certificate":
        return ["duty", "declaration", "audit"], ["measure_condition", "document_requirement"]

    if _VALUE_RE.search(lowered):
        return ["valuation", "landed_cost", "audit"], ["valuation_input"]

    if _DUTY_RE.search(lowered):
        return ["duty", "landed_cost", "declaration", "audit"], ["duty_rate_measure", "landed_cost_component"]

    if _TRADE_GEO_RE.search(lowered):
        return ["duty", "declaration", "audit"], ["origin_or_region"]

    if _PRODUCT_GEO_RE.search(lowered):
        return ["retrieval", "classification", "qa", "audit"], ["origin_or_region"]

    if _EXCLUSION_RE.search(lowered):
        return ["retrieval", "classification", "audit"], ["legal_exclusion"]

    scopes = ["retrieval", "classification", "qa", "audit"]
    roles: list[str] = []
    if _ADDITIONAL_CODE_RE.search(lowered):
        scopes = ["retrieval", "classification", "qa", "duty", "landed_cost", "declaration", "audit"]
        roles.extend(["additional_code", "composition_threshold"])
    elif _COMPOSITION_RE.search(lowered):
        roles.append("composition_threshold" if re.search(r"(protein|fat|sugar|alcohol|abv|content|carbon)", lowered) else "material_composition")
    elif _FORM_RE.search(lowered):
        roles.append("form_presentation")
    elif _USE_RE.search(lowered):
        roles.append("function_use")
    elif _PACKAGING_RE.search(lowered):
        roles.append("packaging_quantity")
    elif _IDENTITY_RE.search(lowered):
        roles.append("product_identity")

    if not roles:
        roles.append("product_identity")
    return scopes, _dedupe(roles)


def facet_labels(source: str, key: str, value: Any | None = None) -> tuple[list[str], list[str]]:
    return profile_labels(*_facet_labels_full(source, key, value))


def _edge_labels_full(
    type_: str,
    authority_tier: int | None = None,
    *,
    source: str = "",
    edge_id: str = "",
    scope: str = "",
) -> tuple[list[str], list[str]]:
    """Return (use_scopes, evidence_roles) for a kg_edge row."""
    etype = (type_ or "").lower()
    src = (source or "").lower()
    eid = (edge_id or "").lower()

    if etype == "duty_treatment":
        return ["duty", "landed_cost", "audit"], ["duty_rate_measure", "landed_cost_component"]

    if etype == "footnote":
        return ["declaration", "audit"], ["footnote", "document_requirement"]

    if etype == "hsen_section_general":
        return ["classification", "audit"], ["interpretive_guidance"]

    if etype == "hsen_general":
        return ["classification", "audit"], ["interpretive_guidance"]

    if etype == "hsen_heading":
        return ["retrieval", "classification", "audit"], ["heading_guidance", "interpretive_guidance"]

    if eid.startswith("gir_") or etype == "classification_order":
        return ["classification", "audit"], ["classification_order"]

    if etype == "exclusion":
        return ["retrieval", "classification", "audit"], ["legal_exclusion"]

    if etype == "inclusion":
        return ["retrieval", "classification", "qa", "audit"], ["legal_inclusion"]

    if etype == "definition":
        return ["retrieval", "classification", "qa", "audit"], ["legal_definition"]

    if etype == "discriminator":
        return ["retrieval", "classification", "qa", "audit"], ["product_identity", "classification_rationale"]

    if etype == "rationale" or eid.startswith("atar_") or "atar" in src:
        return ["retrieval", "classification", "audit"], ["classification_rationale"]

    tier = int(authority_tier or 8)
    if tier <= 3:
        return ["classification", "audit"], ["interpretive_guidance"]
    return ["audit"], ["unknown"]


def edge_labels(
    type_: str,
    authority_tier: int | None = None,
    *,
    source: str = "",
    edge_id: str = "",
    scope: str = "",
) -> tuple[list[str], list[str]]:
    return profile_labels(
        *_edge_labels_full(type_, authority_tier, source=source, edge_id=edge_id, scope=scope)
    )


def _hydration_labels_full(kind: str) -> tuple[list[str], list[str]]:
    """Label transient hydration evidence blobs."""
    lowered = (kind or "").lower()
    if lowered == "facet":
        return ["audit"], ["product_identity"]
    if lowered in {"measure"}:
        return ["duty", "landed_cost", "declaration", "audit"], ["duty_rate_measure", "landed_cost_component"]
    if lowered in {"footnote"}:
        return ["declaration", "audit"], ["footnote", "document_requirement"]
    if lowered in {"chapter_note", "section_note", "gir", "kg_note"}:
        return ["classification", "audit"], ["interpretive_guidance"]
    if lowered == "hsen":
        return ["classification", "audit"], ["interpretive_guidance"]
    if lowered == "atar":
        return ["retrieval", "classification", "audit"], ["classification_rationale"]
    return ["audit"], ["unknown"]


def hydration_labels(kind: str) -> tuple[list[str], list[str]]:
    return profile_labels(*_hydration_labels_full(kind))


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out
