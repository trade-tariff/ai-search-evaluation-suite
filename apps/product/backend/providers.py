from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

from _retry import with_retry_and_limit
from schemas import CompletionResult, ModelConfig, ProviderType


class BaseLLMProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        model_config: ModelConfig,
        prompt_index: int,
    ) -> CompletionResult:
        pass

    def _calc_cost(self, input_tokens: int, output_tokens: int, mc: ModelConfig) -> float:
        return (
            input_tokens * mc.input_cost_per_million / 1_000_000
            + output_tokens * mc.output_cost_per_million / 1_000_000
        )


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, api_key: str):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key)

    async def complete(self, messages, model_config, prompt_index):
        kwargs: dict = {
            "model": model_config.model_id,
            "messages": messages,
        }
        if model_config.reasoning_effort:
            kwargs["reasoning_effort"] = model_config.reasoning_effort

        # Route reasoning-heavy calls to a tighter pool so they don't starve
        # lighter calls and trip Cloudflare throttling. See _retry.py notes.
        bucket = (
            "openai_reasoning"
            if (model_config.reasoning_effort or "").lower() in ("high", "xhigh")
            else "openai"
        )

        start = time.perf_counter()
        try:
            resp = await with_retry_and_limit(
                bucket,
                lambda: self.client.chat.completions.create(**kwargs),
            )
        except Exception as exc:
            return CompletionResult(
                model_id=model_config.id,
                prompt_index=prompt_index,
                response_text="",
                latency_ms=(time.perf_counter() - start) * 1000,
                error=str(exc),
            )
        latency = (time.perf_counter() - start) * 1000

        usage = resp.usage or type("U", (), {"prompt_tokens": 0, "completion_tokens": 0})()
        inp = getattr(usage, "prompt_tokens", 0) or 0
        out = getattr(usage, "completion_tokens", 0) or 0
        text = resp.choices[0].message.content or ""

        return CompletionResult(
            model_id=model_config.id,
            prompt_index=prompt_index,
            response_text=text,
            input_tokens=inp,
            output_tokens=out,
            latency_ms=latency,
            cost=self._calc_cost(inp, out, model_config),
        )


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, api_key: str):
        import anthropic

        self.client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(self, messages, model_config, prompt_index):
        system = None
        user_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system = msg["content"]
            else:
                user_messages.append(msg)

        kwargs: dict = {
            "model": model_config.model_id,
            "messages": user_messages,
            "max_tokens": 4096,
        }
        if system:
            kwargs["system"] = system
        if model_config.thinking_budget:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": model_config.thinking_budget,
            }
            kwargs["max_tokens"] = model_config.thinking_budget + 4096

        start = time.perf_counter()
        try:
            resp = await with_retry_and_limit(
                "anthropic",
                lambda: self.client.messages.create(**kwargs),
            )
        except Exception as exc:
            return CompletionResult(
                model_id=model_config.id,
                prompt_index=prompt_index,
                response_text="",
                latency_ms=(time.perf_counter() - start) * 1000,
                error=str(exc),
            )
        latency = (time.perf_counter() - start) * 1000

        inp = resp.usage.input_tokens
        out = resp.usage.output_tokens
        text = ""
        for block in resp.content:
            if block.type == "text":
                text = block.text
                break

        return CompletionResult(
            model_id=model_config.id,
            prompt_index=prompt_index,
            response_text=text,
            input_tokens=inp,
            output_tokens=out,
            latency_ms=latency,
            cost=self._calc_cost(inp, out, model_config),
        )


class GoogleProvider(BaseLLMProvider):
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def complete(self, messages, model_config, prompt_index):
        import google.generativeai as genai

        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(model_config.model_id)

        parts = [msg["content"] for msg in messages]
        prompt_text = "\n\n".join(parts)

        start = time.perf_counter()
        # Google free-tier 429s need long backoffs (per-minute quotas, often
        # require 30-60s wait). Bigger base_delay_s than default.
        try:
            resp = await with_retry_and_limit(
                "google",
                lambda: asyncio.to_thread(model.generate_content, prompt_text),
                max_attempts=3,
                base_delay_s=15.0,
            )
        except Exception as exc:
            return CompletionResult(
                model_id=model_config.id,
                prompt_index=prompt_index,
                response_text="",
                latency_ms=(time.perf_counter() - start) * 1000,
                error=str(exc),
            )
        latency = (time.perf_counter() - start) * 1000

        meta = getattr(resp, "usage_metadata", None)
        inp = getattr(meta, "prompt_token_count", 0) or 0
        out = getattr(meta, "candidates_token_count", 0) or 0
        text = resp.text or ""

        return CompletionResult(
            model_id=model_config.id,
            prompt_index=prompt_index,
            response_text=text,
            input_tokens=inp,
            output_tokens=out,
            latency_ms=latency,
            cost=self._calc_cost(inp, out, model_config),
        )


class OpenAICompatibleProvider(BaseLLMProvider):
    """Works with Groq, DeepSeek, Mistral, Together, Fireworks, etc."""

    def __init__(self, api_key: str, base_url: str):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def complete(self, messages, model_config, prompt_index):
        kwargs: dict = {
            "model": model_config.model_id,
            "messages": messages,
        }
        if model_config.reasoning_effort:
            kwargs["reasoning_effort"] = model_config.reasoning_effort

        start = time.perf_counter()
        try:
            resp = await with_retry_and_limit(
                "openai_compatible",
                lambda: self.client.chat.completions.create(**kwargs),
            )
        except Exception as exc:
            return CompletionResult(
                model_id=model_config.id,
                prompt_index=prompt_index,
                response_text="",
                latency_ms=(time.perf_counter() - start) * 1000,
                error=str(exc),
            )
        latency = (time.perf_counter() - start) * 1000

        usage = resp.usage or type("U", (), {"prompt_tokens": 0, "completion_tokens": 0})()
        inp = getattr(usage, "prompt_tokens", 0) or 0
        out = getattr(usage, "completion_tokens", 0) or 0
        text = resp.choices[0].message.content or ""

        return CompletionResult(
            model_id=model_config.id,
            prompt_index=prompt_index,
            response_text=text,
            input_tokens=inp,
            output_tokens=out,
            latency_ms=latency,
            cost=self._calc_cost(inp, out, model_config),
        )


_provider_cache: dict[str, BaseLLMProvider] = {}


def get_provider(model_config: ModelConfig, api_keys: dict[str, str | None]) -> BaseLLMProvider:
    cache_key = f"{model_config.provider}:{model_config.base_url or 'default'}"
    if cache_key in _provider_cache:
        return _provider_cache[cache_key]

    provider: BaseLLMProvider
    if model_config.provider == ProviderType.OPENAI:
        key = api_keys.get("openai") or ""
        provider = OpenAIProvider(api_key=key)
    elif model_config.provider == ProviderType.ANTHROPIC:
        key = api_keys.get("anthropic") or ""
        provider = AnthropicProvider(api_key=key)
    elif model_config.provider == ProviderType.GOOGLE:
        key = api_keys.get("google") or ""
        provider = GoogleProvider(api_key=key)
    elif model_config.provider == ProviderType.OPENAI_COMPATIBLE:
        env_name = model_config.api_key_env or ""
        key = api_keys.get(env_name) or ""
        provider = OpenAICompatibleProvider(api_key=key, base_url=model_config.base_url or "")
    else:
        raise ValueError(f"Unknown provider: {model_config.provider}")

    _provider_cache[cache_key] = provider
    return provider


def clear_provider_cache() -> None:
    _provider_cache.clear()
