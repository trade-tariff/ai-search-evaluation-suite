"""Optional research entrypoint, separate from the default worker launcher."""
from __future__ import annotations

import importlib

from fastapi.routing import APIRoute

from .research_app_base import app


def _registered_route_methods() -> set[tuple[str, str]]:
    registered: set[tuple[str, str]] = set()
    for route in app.router.routes:
        if isinstance(route, APIRoute):
            for method in route.methods or set():
                registered.add((method, route.path))
    return registered


_DEPLOYABLE_WORKBENCH_EXACT_PATHS = {
    "/api/config",
    "/api/sections",
    "/api/retrieval/experiments",
    "/api/retrieval/top-experiment",
    "/api/retrieval/try",
    "/api/hydration/candidates",
    "/eval/matrix",
    "/eval/matrix.csv",
    "/eval/e2e-matrix",
}
_DEPLOYABLE_WORKBENCH_PREFIXES = (
    "/api/prompts",
    "/api/search/",
    "/api/intercepts/",
    "/api/complexity/",
    "/api/atar/",
    "/api/benchmark/",
    "/api/kg/",
)


def _deployable_workbench_route(path: str) -> bool:
    return path in _DEPLOYABLE_WORKBENCH_EXACT_PATHS or any(
        path.startswith(prefix) for prefix in _DEPLOYABLE_WORKBENCH_PREFIXES
    )


def _install_full_workbench_routes() -> None:
    """Mount the product API surface into this research app.

    The app owns health, eval-job control, spend-aware retrieval trials, and
    matrix routes. Product routes are copied in one process, with non-deployed
    product surfaces filtered out at registration time.
    """
    installed = _registered_route_methods()
    route_sources = ("main",)
    for module_name in route_sources:
        module = importlib.import_module(module_name)
        source_app = getattr(module, "app")
        for route in source_app.router.routes:
            if not isinstance(route, APIRoute):
                continue
            if not _deployable_workbench_route(route.path):
                continue
            methods = route.methods or set()
            if any((method, route.path) in installed for method in methods):
                continue
            app.router.routes.append(route)
            for method in methods:
                installed.add((method, route.path))


_install_full_workbench_routes()
