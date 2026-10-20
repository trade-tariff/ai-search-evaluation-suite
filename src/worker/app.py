"""Backend-driven evaluation worker for Trade Tariff Admin."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request

from .auth import auth_enabled, install_optional_auth
from .ingress import router as evaluation_router
from .logging_config import configure_logging, is_healthcheck_path

configure_logging()
_access_log = logging.getLogger("ecs.access")

PACKAGE_ROOT = Path(__file__).resolve().parent

app = FastAPI(
    title="AI Search Evaluation Worker",
    description="Backend-driven evaluation worker for Trade Tariff Admin.",
)
install_optional_auth(app, realm="AI Search Evaluation Suite")
app.include_router(evaluation_router)


@app.middleware("http")
async def log_http_requests(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    path = request.url.path
    if is_healthcheck_path(path):
        return response
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    _access_log.info(
        "[%s] %s %s",
        response.status_code,
        request.method,
        path,
        extra={
            "method": request.method,
            "path": path,
            "status": response.status_code,
            "duration": duration_ms,
        },
    )
    return response


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "auth_enabled": auth_enabled(),
        "product_backend_present": (PACKAGE_ROOT / "backend").is_dir(),
    }


@app.get("/api/live")
def live() -> dict:
    return {"status": "ok"}
