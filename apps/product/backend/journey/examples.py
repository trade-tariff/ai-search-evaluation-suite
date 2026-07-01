"""E2E trader journey examples and classification config."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import local_db


DATA_DIR = Path(__file__).parent / "data"
EXAMPLES_PATH = DATA_DIR / "trader_journey_prompts.json"


@lru_cache(maxsize=1)
def _payload() -> dict:
    return json.loads(EXAMPLES_PATH.read_text())


def default_classify_config() -> dict:
    """Best documented e2e/Q&A configuration for the full journey."""
    return dict(_payload().get("config") or {})


def list_examples(persona: str | None = None) -> dict:
    live_examples = local_db.journey_demo_examples(persona=persona)
    selected_persona = local_db._normal_demo_persona(persona)
    if live_examples:
        return {
            "config": default_classify_config(),
            "examples": live_examples,
            "source": "live_kg",
            "persona": selected_persona,
            "personas": local_db.journey_demo_personas(),
            "note": "Examples selected from kg.eval_gold; fact_count is the number of structured commodity facts attached to the expected commodity code in kg.commodity_facets. facet_count is kept as a legacy alias.",
        }
    return {
        "config": default_classify_config(),
        "examples": list(_payload().get("examples") or []),
        "source": "fixture",
        "persona": None,
        "personas": [],
        "note": "Live tariff_db is unavailable; using the full-app offline fallback examples.",
    }
