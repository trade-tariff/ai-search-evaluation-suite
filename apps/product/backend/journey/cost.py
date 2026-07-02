"""Best-effort daily OpenAI spend tracker for the demo.

Monkeypatches the OpenAI SDK create() methods so every chat / responses /
embeddings call adds its token usage to a per-day USD ESTIMATE (not billing
grade). Exposed via /api/cost so the UI can flash a banner past the cap.

Past the cap, new provider calls are REFUSED (COST_CAP_ENFORCED=0 reverts to
banner-only). Known limit: the counter is in-memory, so a container restart
resets the day's total.
"""
import os
import threading
from datetime import date

DEFAULT_THRESHOLD_USD = 5.0


def _threshold():
    try:
        return float(os.environ.get("COST_THRESHOLD_USD") or DEFAULT_THRESHOLD_USD)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_USD

# Rough $ per 1M tokens (input, output) - ESTIMATES; tune to real pricing.
_PRICES = {
    "gpt-5.5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "o": (1.10, 4.40),
}
_DEFAULT = (1.0, 4.0)

_lock = threading.Lock()
_state = {"day": None, "usd": 0.0, "calls": 0}


def _price(model):
    m = (model or "").strip()
    for key, val in _PRICES.items():
        if m.startswith(key):
            return val
    return _DEFAULT


def _tok(usage, *names):
    for n in names:
        v = getattr(usage, n, None)
        if v is None and isinstance(usage, dict):
            v = usage.get(n)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _roll(today):
    if _state["day"] != today:
        _state["day"] = today
        _state["usd"] = 0.0
        _state["calls"] = 0


def record(model, usage):
    if usage is None:
        return
    try:
        pin, pout = _price(model)
        it = _tok(usage, "prompt_tokens", "input_tokens")
        ot = _tok(usage, "completion_tokens", "output_tokens")
        cost = it / 1e6 * pin + ot / 1e6 * pout
        with _lock:
            _roll(date.today().isoformat())
            _state["usd"] += cost
            _state["calls"] += 1
    except Exception:
        pass


def snapshot():
    with _lock:
        _roll(date.today().isoformat())
        usd = round(_state["usd"], 4)
        calls = _state["calls"]
        day = _state["day"]
    cap = _threshold()
    return {
        "day": day,
        "usd": usd,
        "calls": calls,
        "threshold_usd": cap,
        "over": usd > cap,
        "estimated": True,
    }


def is_over() -> bool:
    with _lock:
        _roll(date.today().isoformat())
        return _state["usd"] > _threshold()


def _enforced() -> bool:
    return os.environ.get("COST_CAP_ENFORCED", "1").strip() != "0"


_installed = False


def install():
    global _installed
    if _installed:
        return
    _installed = True

    def wrap(orig):
        def inner(self, *a, **k):
            if _enforced() and is_over():
                raise RuntimeError(
                    "Daily AI spend cap reached (estimated $%.2f limit). New "
                    "provider calls are blocked until tomorrow; raise "
                    "COST_THRESHOLD_USD or set COST_CAP_ENFORCED=0 to override."
                    % _threshold()
                )
            r = orig(self, *a, **k)
            try:
                record(k.get("model"), getattr(r, "usage", None))
            except Exception:
                pass
            return r
        return inner

    try:
        from openai.resources.chat import completions as _c
        _c.Completions.create = wrap(_c.Completions.create)
    except Exception:
        pass
    try:
        from openai.resources import responses as _r
        _r.Responses.create = wrap(_r.Responses.create)
    except Exception:
        pass
    try:
        from openai.resources import embeddings as _e
        _e.Embeddings.create = wrap(_e.Embeddings.create)
    except Exception:
        pass
