from __future__ import annotations

import base64
import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def _csv_env(name: str) -> set[str]:
    return {part.strip() for part in os.environ.get(name, "").split(",") if part.strip()}


def auth_enabled() -> bool:
    return bool(
        os.environ.get("AI_FAN_OUT_BEARER_TOKEN")
        or (
            os.environ.get("AI_FAN_OUT_BASIC_AUTH_USER")
            and os.environ.get("AI_FAN_OUT_BASIC_AUTH_PASSWORD")
        )
    )


def install_optional_auth(app: FastAPI, *, realm: str = "AI Fan-Out") -> None:
    """Install deployment auth when auth env vars are configured."""

    @app.middleware("http")
    async def _optional_auth(request: Request, call_next):
        if not auth_enabled() or request.method == "OPTIONS":
            return await call_next(request)

        public_paths = _csv_env("AI_FAN_OUT_AUTH_PUBLIC_PATHS")
        if request.url.path in public_paths:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        bearer = os.environ.get("AI_FAN_OUT_BEARER_TOKEN") or ""
        if bearer and header.lower().startswith("bearer "):
            supplied = header[7:].strip()
            if hmac.compare_digest(supplied, bearer):
                return await call_next(request)

        user = os.environ.get("AI_FAN_OUT_BASIC_AUTH_USER") or ""
        password = os.environ.get("AI_FAN_OUT_BASIC_AUTH_PASSWORD") or ""
        if user and password and header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
                supplied_user, supplied_password = decoded.split(":", 1)
            except Exception:
                supplied_user, supplied_password = "", ""
            if hmac.compare_digest(supplied_user, user) and hmac.compare_digest(
                supplied_password, password
            ):
                return await call_next(request)

        headers = {}
        if user and password:
            headers["WWW-Authenticate"] = f'Basic realm="{realm}"'
        return JSONResponse(
            {"detail": "Authentication required"},
            status_code=401,
            headers=headers,
        )
