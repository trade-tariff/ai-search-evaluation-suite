"""Evidence consumer scopes and semantic roles for the deployable KG."""
from __future__ import annotations

import os
import re
from typing import Any


USE_SCOPE_DEFINITIONS: dict[str, tuple[str, str]] = {
    "retrieval": ("Retrieval", "Candidate retrieval and ranking from product text."),
    "classification": ("Classification", "Classification reasoning and final candidate selection."),
    "qa": ("Q&A", "Classification questions and answer options."),
    "audit": ("Audit", "Evidence display, provenance, explanation, and debugging."),
}


EVIDENCE_ROLE_DEFINITIONS: dict[str, tuple[str, str]] = {
    "alias": ("Alias", "Common vocabulary that points to a commodity code."),
    "product_identity": ("Product Identity", "What the goods are, product family, or category."),
    "material_composition": ("Material / Composition", "Material, ingredient, substance, or composition facts."),
    "form_presentation": ("Form / Presentation", "Physical form, processing state, construction, or presentation."),
    "function_use": ("Function / Use", "Intended use, function, end use, or application."),
    "packaging_quantity": ("Packaging / Quantity", "Pack size, container, volume, weight, dimensions, or thresholds."),
    "composition_threshold": ("Composition Threshold", "Threshold values such as alcohol, fat, protein, sugar, starch, or solids."),
    "additional_code": ("Additional Code", "Additional-code input or result."),
    "origin_or_region": ("Origin / Region", "Product origin, region, appellation, or protected designation."),
    "legal_definition": ("Legal Definition", "Legal definition or term meaning from GIRs, notes, or HSEN."),
    "legal_inclusion": ("Legal Inclusion", "Rule saying goods are included in a scope."),
    "legal_exclusion": ("Legal Exclusion", "Rule saying goods are excluded from a scope."),
    "classification_order": ("Classification Order", "Ordering rule such as GIR order or chapter/section precedence."),
    "classification_rationale": ("Rationale", "Ruling or extracted rationale for classification."),
    "interpretive_guidance": ("Interpretive Guidance", "HSEN or other interpretive guidance."),
    "heading_guidance": ("Heading Guidance", "Heading-level interpretive guidance."),
    "footnote": ("Footnote", "Footnote text attached to a commodity or measure."),
    "index_text": ("Index Text", "Search-only text artifact, not itself a fact."),
    "unknown": ("Unknown", "Unclassified evidence role."),
}


DEPLOYABLE_USE_SCOPES = tuple(USE_SCOPE_DEFINITIONS)
DEPLOYABLE_CONSUMER_USE_SCOPES = ("retrieval", "classification", "qa")
DEPLOYABLE_EVIDENCE_ROLES = tuple(EVIDENCE_ROLE_DEFINITIONS)


def kg_label_profile() -> str:
    return os.environ.get("AI_FAN_OUT_KG_LABEL_PROFILE", "deployable").strip().lower() or "deployable"


def deployable_profile_enabled() -> bool:
    return True


def active_use_scope_definitions() -> dict[str, tuple[str, str]]:
    return dict(USE_SCOPE_DEFINITIONS)


def active_evidence_role_definitions() -> dict[str, tuple[str, str]]:
    return dict(EVIDENCE_ROLE_DEFINITIONS)


def profile_labels(use_scopes: list[str], evidence_roles: list[str]) -> tuple[list[str], list[str]]:
    scoped = [scope for scope in _dedupe(use_scopes) if scope in DEPLOYABLE_USE_SCOPES]
    roles = [role for role in _dedupe(evidence_roles) if role in DEPLOYABLE_EVIDENCE_ROLES]
    return scoped or ["audit"], roles or ["unknown"]


_EXCLUSION_RE = re.compile(r"(exclude|excluded|excludes|exclusion)")
_PRODUCT_GEO_RE = re.compile(r"(origin|region|appellation|pdo|pgi|designation)")
_ADDITIONAL_CODE_RE = re.compile(r"(meursing|additional_code|starch|sucrose|glucose|milk_fat|milk_protein|milk_solids)")
_COMPOSITION_RE = re.compile(r"(material|composition|ingredient|component|substance|protein|fat|sugar|alcohol|abv|content|carbon)")
_FORM_RE = re.compile(r"(form|state|processing|process|prepared|presentation|construction|manufactur|coating|fermentation)")
_USE_RE = re.compile(r"(function|use|purpose|application|end_use)")
_PACKAGING_RE = re.compile(r"(package|packing|container|net|weight|volume|size|capacity|dimension|diameter|thickness|length|width|cross_section|strength)")
_IDENTITY_RE = re.compile(r"(product|type|category|name|designation|article|beverage|wine|footwear|head_type)")


def _facet_labels_full(source: str, key: str, value: Any | None = None) -> tuple[list[str], list[str]]:
    src = (source or "").lower()
    lowered = (key or "").lower()

    if src == "search_reference" or lowered == "common_term":
        return ["retrieval", "audit"], ["alias"]
    if _PRODUCT_GEO_RE.search(lowered):
        return ["retrieval", "classification", "qa", "audit"], ["origin_or_region"]
    if _EXCLUSION_RE.search(lowered):
        return ["retrieval", "classification", "audit"], ["legal_exclusion"]

    scopes = ["retrieval", "classification", "qa", "audit"]
    roles: list[str] = []
    if _ADDITIONAL_CODE_RE.search(lowered):
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
    etype = (type_ or "").lower()
    src = (source or "").lower()
    eid = (edge_id or "").lower()

    if etype == "footnote":
        return ["audit"], ["footnote"]
    if etype in {"hsen_section_general", "hsen_general"}:
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
    lowered = (kind or "").lower()
    if lowered == "facet":
        return ["audit"], ["product_identity"]
    if lowered in {"chapter_note", "section_note", "gir", "kg_note", "hsen"}:
        return ["classification", "audit"], ["interpretive_guidance"]
    if lowered == "atar":
        return ["retrieval", "classification", "audit"], ["classification_rationale"]
    if lowered == "footnote":
        return ["audit"], ["footnote"]
    return ["audit"], ["unknown"]


def hydration_labels(kind: str) -> tuple[list[str], list[str]]:
    return profile_labels(*_hydration_labels_full(kind))


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out
