"""Backend-driven evaluation worker for Trade Tariff Admin."""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

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

PRODUCT_FRONTEND_DIST = PRODUCT_APP_ROOT / "frontend" / "dist"
PRODUCT_FRONTEND_ASSETS = PRODUCT_FRONTEND_DIST / "assets"
PRODUCT_FRONTEND_INDEX = PRODUCT_FRONTEND_DIST / "index.html"

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


def _react_index() -> FileResponse:
    if not PRODUCT_FRONTEND_INDEX.exists():
        raise HTTPException(
            404,
            "React app bundle is missing. Build the frontend or rebuild the deployable image.",
        )
    return FileResponse(PRODUCT_FRONTEND_INDEX, media_type="text/html")


app.mount(
    "/assets",
    StaticFiles(directory=str(PRODUCT_FRONTEND_ASSETS), check_dir=False),
    name="workbench-assets",
)


@app.get("/", include_in_schema=False)
def deployable_app() -> FileResponse:
    return _react_index()


@app.get("/workbench", include_in_schema=False)
@app.get("/workbench/", include_in_schema=False)
def workbench_redirect() -> RedirectResponse:
    return RedirectResponse("/", status_code=308)


@app.get("/workbench/{path:path}", include_in_schema=False)
def workbench_spa_redirect(path: str) -> RedirectResponse:
    return workbench_redirect()
