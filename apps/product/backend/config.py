from __future__ import annotations

import json
import os
from pathlib import Path

from schemas import ApiKeys, AppConfig, ModelConfig, ProviderType

CONFIG_PATH = Path(__file__).parent.parent / "data" / "config.json"

DEFAULT_MODELS: list[ModelConfig] = [
    # ══════════════════════════════════════════════════════════
    # TIER 1 PAID - OpenAI, Anthropic, xAI (require API keys + credits)
    # ══════════════════════════════════════════════════════════

    # ── OpenAI: GPT-5.x series (thinking models) ────────────
    # Each thinking model has low/medium/high variants. Only medium enabled by default.
    ModelConfig(
        id="gpt-5.5",
        name="GPT-5.5",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.5",
        reasoning_effort="medium",
        category="tier1_paid",
        input_cost_per_million=5.00,
        output_cost_per_million=30.00,
    ),
    ModelConfig(
        id="gpt-5.4",
        name="GPT-5.4",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.4",
        reasoning_effort="medium",
        category="tier1_paid",
        input_cost_per_million=2.50,
        output_cost_per_million=15.00,
    ),
    ModelConfig(
        id="gpt-5.4-low",
        name="GPT-5.4 (low)",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.4",
        reasoning_effort="low",
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=2.50,
        output_cost_per_million=15.00,
    ),
    ModelConfig(
        id="gpt-5.4-high",
        name="GPT-5.4 (high)",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.4",
        reasoning_effort="high",
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=2.50,
        output_cost_per_million=15.00,
    ),
    ModelConfig(
        id="gpt-5.4-xhigh",
        name="GPT-5.4 (xhigh)",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.4",
        reasoning_effort="xhigh",
        # Pinned gold reference - always runs for every prompt regardless of
        # user's candidate selection, so all fan-out candidates are judged
        # against the same strong reasoner.
        is_panel=True,
        enabled=True,
        category="tier1_paid",
        input_cost_per_million=2.50,
        output_cost_per_million=15.00,
    ),
    ModelConfig(
        id="gpt-5.2",
        name="GPT-5.2",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.2",
        reasoning_effort="medium",
        category="tier1_paid",
        input_cost_per_million=1.75,
        output_cost_per_million=14.00,
    ),
    ModelConfig(
        id="gpt-5.2-low",
        name="GPT-5.2 (low)",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.2",
        reasoning_effort="low",
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=1.75,
        output_cost_per_million=14.00,
    ),
    ModelConfig(
        id="gpt-5.2-high",
        name="GPT-5.2 (high)",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.2",
        reasoning_effort="high",
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=1.75,
        output_cost_per_million=14.00,
    ),
    ModelConfig(
        id="gpt-5.2-xhigh",
        name="GPT-5.2 (xhigh)",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.2",
        reasoning_effort="xhigh",
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=1.75,
        output_cost_per_million=14.00,
    ),
    ModelConfig(
        id="gpt-5.1",
        name="GPT-5.1",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.1",
        reasoning_effort="medium",
        category="tier1_paid",
        input_cost_per_million=1.25,
        output_cost_per_million=10.00,
    ),
    ModelConfig(
        id="gpt-5.1-low",
        name="GPT-5.1 (low)",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.1",
        reasoning_effort="low",
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=1.25,
        output_cost_per_million=10.00,
    ),
    ModelConfig(
        id="gpt-5.1-high",
        name="GPT-5.1 (high)",
        provider=ProviderType.OPENAI,
        model_id="gpt-5.1",
        reasoning_effort="high",
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=1.25,
        output_cost_per_million=10.00,
    ),
    ModelConfig(
        id="gpt-5-mini",
        name="GPT-5 Mini",
        provider=ProviderType.OPENAI,
        model_id="gpt-5-mini",
        category="tier1_paid",
        input_cost_per_million=0.25,
        output_cost_per_million=2.00,
    ),
    ModelConfig(
        id="gpt-5-nano",
        name="GPT-5 Nano",
        provider=ProviderType.OPENAI,
        model_id="gpt-5-nano",
        category="tier1_paid",
        input_cost_per_million=0.05,
        output_cost_per_million=0.40,
    ),

    # ── OpenAI: GPT-4.1 series (non-reasoning) ──────────────
    ModelConfig(
        id="gpt-4.1",
        name="GPT-4.1",
        provider=ProviderType.OPENAI,
        model_id="gpt-4.1",
        category="tier1_paid",
        input_cost_per_million=2.00,
        output_cost_per_million=8.00,
    ),
    ModelConfig(
        id="gpt-4.1-mini",
        name="GPT-4.1 Mini",
        provider=ProviderType.OPENAI,
        model_id="gpt-4.1-mini",
        category="tier1_paid",
        input_cost_per_million=0.40,
        output_cost_per_million=1.60,
    ),
    ModelConfig(
        id="gpt-4.1-nano",
        name="GPT-4.1 Nano",
        provider=ProviderType.OPENAI,
        model_id="gpt-4.1-nano",
        category="tier1_paid",
        input_cost_per_million=0.10,
        output_cost_per_million=0.40,
    ),


    # ── Anthropic ────────────────────────────────────────────
    # Extended thinking variants (thinking_budget) disabled by default.
    ModelConfig(
        id="claude-opus-4.6",
        name="Claude Opus 4.6",
        provider=ProviderType.ANTHROPIC,
        model_id="claude-opus-4-6",
        category="tier1_paid",
        input_cost_per_million=5.00,
        output_cost_per_million=25.00,
    ),
    ModelConfig(
        id="claude-opus-4.6-thinking",
        name="Claude Opus 4.6 (thinking)",
        provider=ProviderType.ANTHROPIC,
        model_id="claude-opus-4-6",
        thinking_budget=10240,
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=5.00,
        output_cost_per_million=25.00,
    ),
    ModelConfig(
        id="claude-sonnet-4.6",
        name="Claude Sonnet 4.6",
        provider=ProviderType.ANTHROPIC,
        model_id="claude-sonnet-4-6",
        is_panel=True,
        category="tier1_paid",
        input_cost_per_million=3.00,
        output_cost_per_million=15.00,
    ),
    ModelConfig(
        id="claude-sonnet-4.6-thinking",
        name="Claude Sonnet 4.6 (thinking)",
        provider=ProviderType.ANTHROPIC,
        model_id="claude-sonnet-4-6",
        thinking_budget=10240,
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=3.00,
        output_cost_per_million=15.00,
    ),
    ModelConfig(
        id="claude-haiku-4.5",
        name="Claude Haiku 4.5",
        provider=ProviderType.ANTHROPIC,
        model_id="claude-haiku-4-5",
        category="tier1_paid",
        input_cost_per_million=1.00,
        output_cost_per_million=5.00,
    ),
    ModelConfig(
        id="claude-sonnet-4.5",
        name="Claude Sonnet 4.5",
        provider=ProviderType.ANTHROPIC,
        model_id="claude-sonnet-4-5",
        category="tier1_paid",
        input_cost_per_million=3.00,
        output_cost_per_million=15.00,
    ),
    ModelConfig(
        id="claude-sonnet-4.5-thinking",
        name="Claude Sonnet 4.5 (thinking)",
        provider=ProviderType.ANTHROPIC,
        model_id="claude-sonnet-4-5",
        thinking_budget=10240,
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=3.00,
        output_cost_per_million=15.00,
    ),

    # ══════════════════════════════════════════════════════════
    # TIER 1 FREE - Google Gemini (free tier via AI Studio)
    # ══════════════════════════════════════════════════════════

    # ── Google Gemini: 3.x series (latest preview) ───────────
    # NOTE: gemini-3.1-pro has NO free tier (limit: 0)
    ModelConfig(
        id="gemini-3.1-pro",
        name="Gemini 3.1 Pro",
        provider=ProviderType.GOOGLE,
        model_id="gemini-3.1-pro-preview",
        category="tier1_paid",
        input_cost_per_million=2.00,
        output_cost_per_million=12.00,
    ),
    ModelConfig(
        id="gemini-3-flash",
        name="Gemini 3 Flash",
        provider=ProviderType.GOOGLE,
        model_id="gemini-3-flash-preview",
        category="tier1_free",
        input_cost_per_million=0.50,
        output_cost_per_million=3.00,
    ),
    ModelConfig(
        id="gemini-3.1-flash-lite",
        name="Gemini 3.1 Flash Lite",
        provider=ProviderType.GOOGLE,
        model_id="gemini-3.1-flash-lite-preview",
        category="tier1_free",
        input_cost_per_million=0.25,
        output_cost_per_million=1.50,
    ),

    # ── Google Gemini: 2.5 series (stable) ───────────────────
    # NOTE: gemini-2.5-pro has NO free tier (limit: 0)
    ModelConfig(
        id="gemini-2.5-pro",
        name="Gemini 2.5 Pro",
        provider=ProviderType.GOOGLE,
        model_id="gemini-2.5-pro",
        is_panel=True,
        category="tier1_paid",
        input_cost_per_million=1.25,
        output_cost_per_million=10.00,
    ),
    ModelConfig(
        id="gemini-2.5-flash",
        name="Gemini 2.5 Flash",
        provider=ProviderType.GOOGLE,
        model_id="gemini-2.5-flash",
        category="tier1_free",
        input_cost_per_million=0.30,
        output_cost_per_million=2.50,
    ),
    ModelConfig(
        id="gemini-2.5-flash-lite",
        name="Gemini 2.5 Flash Lite",
        provider=ProviderType.GOOGLE,
        model_id="gemini-2.5-flash-lite",
        category="tier1_free",
        input_cost_per_million=0.10,
        output_cost_per_million=0.40,
    ),

    # NOTE: gemini-2.0-flash has NO free tier (limit: 0)
    ModelConfig(
        id="gemini-2.0-flash",
        name="Gemini 2.0 Flash",
        provider=ProviderType.GOOGLE,
        model_id="gemini-2.0-flash",
        category="tier1_paid",
        input_cost_per_million=0.10,
        output_cost_per_million=0.40,
    ),

    # ── xAI Grok ($25 free credits on signup) ────────────────
    ModelConfig(
        id="grok-4",
        name="Grok 4",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="grok-4-0709",
        base_url="https://api.x.ai/v1",
        api_key_env="xai",
        category="tier1_paid",
        input_cost_per_million=3.00,
        output_cost_per_million=15.00,
    ),
    ModelConfig(
        id="grok-4.1-fast-reasoning",
        name="Grok 4.1 Fast (Reasoning)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="grok-4-1-fast-reasoning",
        base_url="https://api.x.ai/v1",
        api_key_env="xai",
        category="tier1_paid",
        input_cost_per_million=0.20,
        output_cost_per_million=0.50,
    ),
    ModelConfig(
        id="grok-4.1-fast",
        name="Grok 4.1 Fast",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="grok-4-1-fast-non-reasoning",
        base_url="https://api.x.ai/v1",
        api_key_env="xai",
        category="tier1_paid",
        input_cost_per_million=0.20,
        output_cost_per_million=0.50,
    ),
    ModelConfig(
        id="grok-3-mini",
        name="Grok 3 Mini",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="grok-3-mini",
        base_url="https://api.x.ai/v1",
        api_key_env="xai",
        reasoning_effort="low",
        category="tier1_paid",
        input_cost_per_million=0.30,
        output_cost_per_million=0.50,
    ),
    ModelConfig(
        id="grok-3-mini-high",
        name="Grok 3 Mini (high)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="grok-3-mini",
        base_url="https://api.x.ai/v1",
        api_key_env="xai",
        reasoning_effort="high",
        enabled=False,
        category="tier1_paid",
        input_cost_per_million=0.30,
        output_cost_per_million=0.50,
    ),

    # ══════════════════════════════════════════════════════════
    # TIER 2 - Open-source and other providers (disabled by default)
    # ══════════════════════════════════════════════════════════

    # ── Meta Llama (US open-source, via Groq free tier) ───────
    ModelConfig(
        id="llama-3.3-70b-groq",
        name="Llama 3.3 70B (Groq)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="groq",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    ModelConfig(
        id="llama-3.1-8b-groq",
        name="Llama 3.1 8B (Groq)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="llama-3.1-8b-instant",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="groq",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    # ── Mistral (French/EU) ───────────────────────────────────
    ModelConfig(
        id="mixtral-8x7b-groq",
        name="Mixtral 8x7B (Groq)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="mixtral-8x7b-32768",
        base_url="https://api.groq.com/openai/v1",
        api_key_env="groq",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    ModelConfig(
        id="mistral-small-latest",
        name="Mistral Small (Direct)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="mistral-small-latest",
        base_url="https://api.mistral.ai/v1",
        api_key_env="mistral",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    ModelConfig(
        id="mistral-small-3.1-openrouter",
        name="Mistral Small 3.1 24B (OpenRouter)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="mistralai/mistral-small-3.1-24b-instruct:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="openrouter",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    # ── Google Gemma (open-source) ────────────────────────────
    ModelConfig(
        id="gemma-3-27b-openrouter",
        name="Gemma 3 27B (OpenRouter)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="google/gemma-3-27b-it:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="openrouter",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    # ── Other open-source via OpenRouter ──────────────────────
    ModelConfig(
        id="llama-3.3-70b-openrouter",
        name="Llama 3.3 70B (OpenRouter)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="meta-llama/llama-3.3-70b-instruct:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="openrouter",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    ModelConfig(
        id="hermes-3-405b-openrouter",
        name="Hermes 3 405B (OpenRouter)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="nousresearch/hermes-3-llama-3.1-405b:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="openrouter",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    ModelConfig(
        id="nemotron-70b-openrouter",
        name="NVIDIA Nemotron 70B (OpenRouter)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="nvidia/llama-3.1-nemotron-70b-instruct:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="openrouter",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    # ── Cerebras (US inference provider) ──────────────────────
    ModelConfig(
        id="llama-3.1-8b-cerebras",
        name="Llama 3.1 8B (Cerebras)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="llama3.1-8b",
        base_url="https://api.cerebras.ai/v1",
        api_key_env="cerebras",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    # ── SambaNova (US inference provider) ─────────────────────
    ModelConfig(
        id="llama-3.1-70b-sambanova",
        name="Llama 3.1 70B (SambaNova)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="Meta-Llama-3.1-70B-Instruct",
        base_url="https://api.sambanova.ai/v1",
        api_key_env="sambanova",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    # ── DeepSeek (Chinese) ────────────────────────────────────
    ModelConfig(
        id="deepseek-v3-openrouter",
        name="DeepSeek V3 (OpenRouter)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="deepseek/deepseek-chat-v3-0324:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="openrouter",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    ModelConfig(
        id="deepseek-chat",
        name="DeepSeek V3 (Direct)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key_env="deepseek",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.27,
        output_cost_per_million=1.10,
    ),
    ModelConfig(
        id="deepseek-reasoner",
        name="DeepSeek R1 (Reasoning)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="deepseek-reasoner",
        base_url="https://api.deepseek.com",
        api_key_env="deepseek",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.55,
        output_cost_per_million=2.19,
    ),
    # ── Qwen / Alibaba (Chinese) ─────────────────────────────
    ModelConfig(
        id="qwen3-coder-480b-openrouter",
        name="Qwen3 Coder 480B (OpenRouter)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="qwen/qwen3-coder-480b-a35b-instruct:free",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="openrouter",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
    ModelConfig(
        id="qwen-3-235b-cerebras",
        name="Qwen 3 235B (Cerebras)",
        provider=ProviderType.OPENAI_COMPATIBLE,
        model_id="qwen-3-235b-a22b-instruct-2507",
        base_url="https://api.cerebras.ai/v1",
        api_key_env="cerebras",
        enabled=False,
        category="tier2",
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
    ),
]


def _api_keys_from_env() -> ApiKeys:
    return ApiKeys(
        openai=os.environ.get("OPENAI_API_KEY"),
        anthropic=os.environ.get("ANTHROPIC_API_KEY"),
        google=os.environ.get("GOOGLE_API_KEY"),
        groq=os.environ.get("GROQ_API_KEY"),
        deepseek=os.environ.get("DEEPSEEK_API_KEY"),
        mistral=os.environ.get("MISTRAL_API_KEY"),
        xai=os.environ.get("XAI_API_KEY"),
        openrouter=os.environ.get("OPENROUTER_API_KEY"),
        cerebras=os.environ.get("CEREBRAS_API_KEY"),
        sambanova=os.environ.get("SAMBANOVA_API_KEY"),
    )


def _overlay_env_api_keys(config: AppConfig) -> AppConfig:
    """Use runtime secret env vars without writing them back to config.json."""
    env_keys = _api_keys_from_env().model_dump()
    merged = config.api_keys.model_dump()
    for key, value in env_keys.items():
        if value:
            merged[key] = value
    config.api_keys = ApiKeys(**merged)
    return config


def load_config() -> AppConfig:
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text())
        config = AppConfig(**raw)
        # Merge any new models from DEFAULT_MODELS that are missing in saved config
        saved_ids = {m.id for m in config.models}
        added = []
        for default_m in DEFAULT_MODELS:
            if default_m.id not in saved_ids:
                config.models.append(default_m)
                added.append(default_m.id)
        if added:
            # Re-save so next load is fast
            save_config(config)
        return _overlay_env_api_keys(config)

    # Load API keys from environment
    return AppConfig(api_keys=_api_keys_from_env(), models=DEFAULT_MODELS)


def save_config(config: AppConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(config.model_dump_json(indent=2))
