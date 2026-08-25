"""Per-model OpenAI $/token pricing for cost attribution in this repo's
evaluation/classification tooling.

Mirrors trade-tariff-backend's config/openai_model_pricing.yml exactly (same
model keys, same $/1M-token input/output rates) so a run's total_cost_usd
reported here matches the same real spend Rails reports for its own LLM
calls. There is no shared source between the two repos/languages -- keep
these two files in sync manually if either changes.

A model missing from this table is not an error: usage is still counted by
callers (provider_calls, token totals), just with cost_usd=None and
pricing_known=False -- the same graceful-degradation behaviour
config/openai_model_pricing.yml's own comment documents on the Rails side.

This is deliberately separate from journey/cost.py's `_PRICES` table, which
is an explicitly-labelled rough ESTIMATE for a daily spend-cap banner on a
different (unrelated) demo app -- not accurate enough for the cost
benchmarking this repo's evaluation tooling needs.
"""
from __future__ import annotations

# USD per 1,000,000 tokens. Only input/output rates are ported -- this
# repo's simulator calls never use cached-input or long-context pricing
# tiers, so those extra fields from the Rails YAML aren't needed here.
MODEL_PRICING = {
    "gpt-5.6": {"input": 5.0, "output": 30.0},
    "gpt-5.6-terra": {"input": 2.5, "output": 15.0},
    "gpt-5.6-luna": {"input": 1.0, "output": 6.0},
    "gpt-5.5": {"input": 5.0, "output": 30.0},
    "gpt-5.4": {"input": 2.5, "output": 15.0},
    "gpt-5.2": {"input": 1.75, "output": 14.0},
    "gpt-5.1-2025-11-13": {"input": 1.25, "output": 10.0},
    "gpt-5-2025-08-07": {"input": 1.25, "output": 10.0},
    "gpt-5-mini-2025-08-07": {"input": 0.25, "output": 2.0},
    "gpt-5-nano-2025-08-07": {"input": 0.05, "output": 0.4},
    "gpt-4.1-mini-2025-04-14": {"input": 0.4, "output": 1.6},
    "text-embedding-3-small": {"input": 0.02, "output": 0.0},
}


def calculate_cost(model: str, usage) -> tuple[float | None, bool]:
    """Returns (cost_usd, pricing_known) for one API call's token usage.

    `usage` is whatever the OpenAI SDK response's `.usage` attribute holds
    (a CompletionUsage object with .prompt_tokens/.completion_tokens), or a
    plain dict with the same keys -- accepts either so callers don't need to
    normalize first.
    """
    prices = MODEL_PRICING.get(model)
    if prices is None or usage is None:
        return None, False

    prompt_tokens = _tokens(usage, "prompt_tokens")
    completion_tokens = _tokens(usage, "completion_tokens")
    cost = (prompt_tokens / 1_000_000 * prices["input"]) + (completion_tokens / 1_000_000 * prices["output"])
    return cost, True


def _tokens(usage, name):
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(name)
    return value or 0
