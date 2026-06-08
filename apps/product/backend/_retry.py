"""Shared transient-error retry + per-provider concurrency limiting.

Two separate concerns, one module because they both wrap API calls:

1. Retry - silently re-attempts calls that hit transient network errors or
   5xx/429 responses. The caller decides if the final failure is fatal.
   Default: 3 attempts, exponential backoff at 1s / 2s / 4s.

2. Concurrency - a per-provider asyncio.Semaphore caps how many in-flight
   calls we can have against any one provider at a time. Stops the benchmark
   from firing 50 simultaneous requests at Google on a 3 rps free tier and
   burning the whole quota in one go.

Both are applied together via `with_retry_and_limit(provider_key, callable)`
so every site in the codebase gets consistent behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

# Per-provider concurrency caps. Three OpenAI buckets because reasoning
# models (gpt-5.x high/xhigh) hold connections open 60-120s each and trip
# Cloudflare bot-detection / TPM limits when too many run in parallel.
# Keeping them in their own tight pool avoids the 52%-error catastrophe
# we saw with openai=30 and xhigh multi-pass.
#
#   openai              fast models (gpt-5 nano/mini, gpt-4.x, reasoning=low/medium)
#   openai_reasoning    heavy reasoning (reasoning=high/xhigh)
#   openai_judge        dedicated pool for llm_judge calls so they don't
#                       compete with model/simulator calls for slots
#   openai_embedding    text-embedding-3-small (cheap, can parallelise freely)
#
# Provider code routes calls to the right bucket based on model_config.
_PROVIDER_CAPS: dict[str, int] = {
    "openai": 20,              # non-reasoning / light-reasoning OpenAI calls
    "openai_reasoning": 6,     # xhigh/high reasoning - these are the connection hogs
    "openai_judge": 4,         # dedicated, so judges don't starve when models run hot
    "openai_embedding": 20,    # cheap, short-lived
    "anthropic": 10,           # Paid tier
    "google": 3,               # Free tier is tight
    "openai_compatible": 10,   # Grok/etc
    "xai": 10,
    "openrouter": 10,
    "groq": 5,
    "deepseek": 5,
    "mistral": 5,
    "cerebras": 3,
    "sambanova": 3,
    "default": 5,
}

_SEMAPHORES: dict[str, asyncio.Semaphore] = {}


def _semaphore_for(provider: str) -> asyncio.Semaphore:
    key = provider or "default"
    if key not in _SEMAPHORES:
        cap = _PROVIDER_CAPS.get(key, _PROVIDER_CAPS["default"])
        _SEMAPHORES[key] = asyncio.Semaphore(cap)
    return _SEMAPHORES[key]


def get_concurrency_cap(provider: str) -> int:
    return _PROVIDER_CAPS.get(provider or "default", _PROVIDER_CAPS["default"])


# Transient error heuristic: match by error class name + substrings. This is
# deliberately string-based so we don't have to import httpx/openai/anthropic
# error hierarchies in this module. If the error looks transient, retry.
_TRANSIENT_SUBSTRINGS = (
    "connection error",
    "connection reset",
    "connection aborted",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "429",
    "500 internal",
    "502",
    "503",
    "504",
    "overloaded",
    "capacity",
    "internal server error",
    "try again",
)

_TRANSIENT_CLASS_NAMES = (
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectionError",
    "ServerDisconnectedError",
    "ServiceUnavailableError",
    "TimeoutError",
)


def _is_transient(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name in _TRANSIENT_CLASS_NAMES:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in _TRANSIENT_SUBSTRINGS)


async def with_retry_and_limit(
    provider: str,
    call: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 5.0,  # 5s / 10s / 20s - up from 1s to give OpenAI
                                 # time to recover from Cloudflare throttling
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> Any:
    """Run `call()` under the provider's concurrency semaphore with retry on
    transient failures. Returns the result of the successful call, or raises
    the final exception after `max_attempts`.

    `on_retry(attempt_number, exc)` is invoked after each failed attempt
    before sleeping; use it to emit telemetry (SSE, logs) about retries.
    """
    sem = _semaphore_for(provider)
    last_exc: BaseException | None = None
    async with sem:
        for attempt in range(1, max_attempts + 1):
            try:
                return await call()
            except Exception as exc:
                last_exc = exc
                if attempt >= max_attempts or not _is_transient(exc):
                    raise
                if on_retry is not None:
                    try:
                        on_retry(attempt, exc)
                    except Exception:
                        pass
                delay = base_delay_s * (2 ** (attempt - 1))
                log.warning(
                    "Transient error on %s (attempt %d/%d): %s - retrying in %.1fs",
                    provider, attempt, max_attempts, type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
    # Should not reach here; kept for type-checker sanity.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry loop exited without result")
