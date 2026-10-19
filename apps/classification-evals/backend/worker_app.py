"""Backend-driven evaluation worker for Trade Tariff Admin."""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Request

from .auth import auth_enabled, install_optional_auth
from .logging_config import configure_logging, is_healthcheck_path

configure_logging()
_access_log = logging.getLogger("ecs.access")

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parents[1]
PRODUCT_APP_ROOT = Path(
    os.environ.get("PRODUCT_APP_ROOT") or REPO_ROOT / "apps" / "product"
).resolve()
PRODUCT_BACKEND = PRODUCT_APP_ROOT / "backend"
if str(PRODUCT_BACKEND) not in sys.path:
    sys.path.insert(0, str(PRODUCT_BACKEND))

from evaluation_ingress import router as evaluation_router

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
        "product_backend_present": PRODUCT_BACKEND.exists(),
    }


@app.get("/api/live")
def live() -> dict:
    return {"status": "ok"}
