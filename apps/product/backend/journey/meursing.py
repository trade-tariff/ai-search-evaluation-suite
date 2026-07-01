"""Deterministic Meursing/additional-code helper.

The UK Global Tariff simplified GB Meursing duty handling, but GOV.UK still
publishes a Meursing lookup and Northern Ireland / EU-style handoffs can need
the composition facts. Keep this local and deterministic: collect the four
percentages, resolve the official band path, and map known demo paths to the
additional code without calling a provider.
"""
from __future__ import annotations

from typing import Any, Optional


Band = tuple[str, float, Optional[float], str]

STARCH_GLUCOSE_BANDS: list[Band] = [
    ("0", 0, 4.99, "0 - 4.99"),
    ("5", 5, 24.99, "5 - 24.99"),
    ("25", 25, 49.99, "25 - 49.99"),
    ("50", 50, 74.99, "50 - 74.99"),
    ("75", 75, None, "75 or more"),
]

SUCROSE_BANDS: list[Band] = [
    ("0", 0, 4.99, "0 - 4.99"),
    ("5", 5, 29.99, "5 - 29.99"),
    ("30", 30, 49.99, "30 - 49.99"),
    ("50", 50, 69.99, "50 - 69.99"),
    ("70", 70, None, "70 or more"),
]

MILK_FAT_BANDS: list[Band] = [
    ("0", 0, 1.49, "0 - 1.49"),
    ("1", 1.5, 2.99, "1.5 - 2.99"),
    ("3", 3, 5.99, "3 - 5.99"),
    ("6", 6, 8.99, "6 - 8.99"),
    ("9", 9, 11.99, "9 - 11.99"),
    ("12", 12, 17.99, "12 - 17.99"),
    ("18", 18, 25.99, "18 - 25.99"),
    ("26", 26, 39.99, "26 - 39.99"),
    ("40", 40, 54.99, "40 - 54.99"),
    ("55", 55, 69.99, "55 - 69.99"),
    ("70", 70, 84.99, "70 - 84.99"),
    ("85", 85, None, "85 or more"),
]

MILK_PROTEIN_BANDS: list[Band] = [
    ("0", 0, 2.49, "0 - 2.49"),
    ("2", 2.5, 5.99, "2.5 - 5.99"),
    ("6", 6, 17.99, "6 - 17.99"),
    ("18", 18, 29.99, "18 - 29.99"),
    ("30", 30, 59.99, "30 - 59.99"),
    ("60", 60, None, "60 or more"),
]

# Deterministic demo mapping verified against the GOV.UK lookup path:
# /additional-commodity-code/y/5/5/0/6 -> 7046.
KNOWN_CODES: dict[tuple[str, str, str, str], str] = {
    ("5", "5", "0", "6"): "7046",
}


def _get(obj: Any, key: str) -> Optional[float | str]:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _band(value: Optional[float], bands: list[Band]) -> Optional[dict]:
    if value is None:
        return None
    v = float(value)
    for code, low, high, label in bands:
        if v >= low and (high is None or v <= high):
            return {"value": code, "label": label}
    return None


def resolve_meursing(inputs: Any) -> dict:
    """Resolve composition percentages into a deterministic additional-code result."""
    starch = _band(_get(inputs, "starch_glucose_pct"), STARCH_GLUCOSE_BANDS)
    sucrose = _band(_get(inputs, "sucrose_invert_isoglucose_pct"), SUCROSE_BANDS)
    milk_fat = _band(_get(inputs, "milk_fat_pct"), MILK_FAT_BANDS)
    milk_protein = _band(_get(inputs, "milk_protein_pct"), MILK_PROTEIN_BANDS)

    missing = [
        name
        for name, band in (
            ("starch_glucose_pct", starch),
            ("sucrose_invert_isoglucose_pct", sucrose),
            ("milk_fat_pct", milk_fat),
            ("milk_protein_pct", milk_protein),
        )
        if band is None
    ]
    if missing:
        return {
            "additional_code": _get(inputs, "additional_code"),
            "code_type": "Meursing",
            "complete": False,
            "missing_fields": missing,
            "lookup_url": "https://www.gov.uk/additional-commodity-code",
            "note": "Composition data is incomplete; use the GOV.UK lookup if an additional code is required.",
        }

    key = (
        str(starch["value"]),
        str(sucrose["value"]),
        str(milk_fat["value"]),
        str(milk_protein["value"]),
    )
    additional_code = str(KNOWN_CODES.get(key) or "")
    lookup_path = "/additional-commodity-code/y/" + "/".join(key)
    return {
        "additional_code": additional_code or None,
        "code_type": "Meursing",
        "complete": True,
        "lookup_path": lookup_path,
        "lookup_url": "https://www.gov.uk" + lookup_path,
        "component_percentages": {
            "starch_glucose_pct": _get(inputs, "starch_glucose_pct"),
            "sucrose_invert_isoglucose_pct": _get(inputs, "sucrose_invert_isoglucose_pct"),
            "milk_fat_pct": _get(inputs, "milk_fat_pct"),
            "milk_protein_pct": _get(inputs, "milk_protein_pct"),
        },
        "bands": {
            "starch_glucose": starch,
            "sucrose_invert_isoglucose": sucrose,
            "milk_fat": milk_fat,
            "milk_protein": milk_protein,
        },
        "note": (
            "Composition captured for Meursing/additional-code handoff. "
            "In this demo the resolved code is carried into the declaration handoff."
        ),
    }
