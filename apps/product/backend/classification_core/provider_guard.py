"""Runtime guard for provider-backed classification eval calls.

Credentials can be present on a demo or EC2 host for other parts of the app, but
classification eval subprocesses should not spend credits unless explicitly enabled.
"""
from __future__ import annotations

import os


TRUE_VALUES = {"1", "true", "yes", "on"}


def provider_calls_allowed() -> bool:
    return os.environ.get("CLASSIFICATION_ALLOW_PROVIDER_CALLS", "").strip().lower() in TRUE_VALUES


def openai_allowed() -> bool:
    return provider_calls_allowed() and bool(os.environ.get("OPENAI_API_KEY"))
