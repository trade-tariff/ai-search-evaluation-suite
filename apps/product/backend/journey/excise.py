"""UK Alcohol Duty Reform 2023 calculator.

Picks the right ABV band for the product type, applies Small Producer Relief
and/or Draught Relief where eligible, and returns the LPA-based excise duty.

Real HMRC also handles: SPR taper by annual production volume; very-small
producer flat rate; SPR for split product types; the Feb 2025 indexation
uplift; cross-border movement reliefs. Out of POC scope - rate matrix uses
pre-indexation baseline values for stability.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def _rates() -> dict:
    return json.loads((DATA_DIR / "excise_rates.json").read_text())


def pick_band(abv: float, beverage_type: str) -> dict:
    """Pick the band that applies to (abv, type)."""
    for band in _rates()["bands"]:
        if not (band["abv_min"] <= abv < band["abv_max"]):
            continue
        applies = band.get("applies_to")
        if applies and beverage_type not in applies:
            continue
        return band
    # Default to the highest band if nothing matched (shouldn't happen with the supplied matrix)
    return _rates()["bands"][-1]


def calculate_excise(
    *,
    beverage_type: str,
    abv: float,
    volume_litres: float,
    is_small_producer: bool,
    is_draught: bool,
) -> dict:
    """Calculate alcohol excise duty for a given beverage.

    Returns a dict with the band, raw rate, applied reliefs, effective rate,
    pure-alcohol litres, and the final duty amount.
    """
    band = pick_band(abv, beverage_type)
    base_rate = float(band["rate_per_lpa_gbp"])
    applied_reliefs: list[dict] = []
    rate = base_rate

    rates = _rates()

    if is_small_producer and band["id"] in rates["small_producer_relief"]["applies_to_bands"]:
        spr_pct = rates["small_producer_relief"]["indicative_discount_pct"]
        rate = rate * (1 - spr_pct / 100.0)
        applied_reliefs.append({
            "name": "Small Producer Relief",
            "discount_pct": spr_pct,
            "note": rates["small_producer_relief"]["label"],
        })
    elif is_small_producer:
        applied_reliefs.append({
            "name": "Small Producer Relief",
            "discount_pct": 0,
            "note": "Producer flagged as small but this band is not SPR-eligible (8.5%+ ABV or spirits).",
        })

    if is_draught and band["id"] in rates["draught_relief"]["applies_to_bands"]:
        draught_pct = rates["draught_relief"]["discount_pct"]
        rate = rate * (1 - draught_pct / 100.0)
        applied_reliefs.append({
            "name": "Draught Relief",
            "discount_pct": draught_pct,
            "note": rates["draught_relief"]["label"],
        })
    elif is_draught:
        applied_reliefs.append({
            "name": "Draught Relief",
            "discount_pct": 0,
            "note": "Container marked as draught but this band is not draught-eligible.",
        })

    pure_alcohol_litres = round(volume_litres * abv / 100.0, 4)
    duty = round(rate * pure_alcohol_litres, 2)

    return {
        "band_id": band["id"],
        "band_label": band["label"],
        "base_rate_per_lpa_gbp": base_rate,
        "effective_rate_per_lpa_gbp": round(rate, 4),
        "applied_reliefs": applied_reliefs,
        "pure_alcohol_litres": pure_alcohol_litres,
        "volume_litres": volume_litres,
        "abv": abv,
        "duty_gbp": duty,
    }


def vat_rates() -> dict:
    return _rates()["_vat_rates"]
