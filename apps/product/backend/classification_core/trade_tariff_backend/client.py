"""Async HTTP client for trade-tariff-backend's evaluation APIs. Wraps two
separate base URLs (AdminApi's /uk/admin/search/evaluation/* and
InternalApi's /uk/internal/*) — confirmed via the 2026-08-14 spike and by
reading the actual Rails routes, not assumed to be one uniform prefix.

This module issues NO Postgres queries and imports no local_db/psycopg code —
it is the "deployed" path the AI-1073 design replaces direct DB access with.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx

DEFAULT_BASE_URL = os.environ.get("TRADE_TARIFF_BACKEND_BASE_URL", "http://127.0.0.1:3000")
REQUEST_TIMEOUT_S = float(os.environ.get("TRADE_TARIFF_BACKEND_REQUEST_TIMEOUT_S", "30"))
MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 0.5


class TradeTariffBackendValidationError(Exception):
    """trade-tariff-backend rejected the request as invalid (4xx). Not retried
    — retrying an invalid request would just fail the same way again."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"trade-tariff-backend rejected the request: {status_code} {body!r}")


class TradeTariffBackendUnavailableError(Exception):
    """trade-tariff-backend could not be reached, or kept failing, across
    every retry attempt (connection errors or 5xx). Distinct from
    TradeTariffBackendValidationError so a caller can tell "this input was
    wrong" from "try again later"."""

    def __init__(self, last_error: Exception | str):
        self.last_error = last_error
        super().__init__(f"trade-tariff-backend unavailable after {MAX_ATTEMPTS} attempts: {last_error!r}")


class TradeTariffBackendClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, transport: Optional[httpx.MockTransport] = None):
        self._admin_base = f"{base_url}/uk/admin/search/evaluation"
        self._internal_base = f"{base_url}/uk/internal"
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S, transport=transport)

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        last_error: Exception | str = "unknown error"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if response.status_code < 300:
                    return response.json() if response.content else {}
                if response.status_code < 500:
                    raise TradeTariffBackendValidationError(response.status_code, self._safe_json(response))
                last_error = f"{response.status_code} {self._safe_json(response)!r}"

            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(RETRY_BASE_DELAY_S * (2 ** (attempt - 1)))

        raise TradeTariffBackendUnavailableError(last_error)

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    async def get_gold_queries(self) -> list[dict]:
        """Every gold query, flattened to plain dicts and across every page.

        Flattened (`attributes` merged with `id`) like every other method here,
        because callers such as execute_run read `gold["source_type"]` as a flat
        key — returning raw JSON:API rows made those reads raise KeyError.

        Paginated because the Rails controller defaults to 100 rows per page
        (DEFAULT_PER_PAGE), so a single unpaginated request would silently
        truncate any larger gold set and score only its first 100 queries while
        still reporting the run as completed. 250 is that controller's
        MAX_PER_PAGE, so this asks for the largest page it will serve.
        """
        rows: list[dict] = []
        page = 1
        while True:
            body = await self._request(
                "GET", f"{self._internal_base}/evaluation_gold_queries",
                params={"page": page, "per_page": 250},
            )
            page_rows = body["data"]
            rows.extend(row["attributes"] | {"id": row["id"]} for row in page_rows)

            total_count = body["meta"]["pagination"]["total_count"]
            if len(rows) >= total_count:
                return rows
            if not page_rows:
                # Defensive: an empty page while total_count still claims more
                # rows would otherwise loop forever against the same page.
                return rows
            page += 1

    async def get_atar_ruling(self, ref: str) -> dict:
        body = await self._request("GET", f"{self._internal_base}/atars/{ref}")
        return body["data"]["attributes"]

    async def create_experiment(self, name: str) -> dict:
        body = await self._request(
            "POST", f"{self._admin_base}/experiments",
            json={"data": {"attributes": {"name": name}}},
        )
        return body["data"]["attributes"] | {"id": body["data"]["id"]}

    async def create_run(self, experiment_id: str, run_time_overrides: dict, idempotency_key: str) -> dict:
        body = await self._request(
            "POST", f"{self._admin_base}/runs",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "data": {
                    "attributes": {
                        "experiment_id": experiment_id,
                        "triggered_by": "ai-search-evaluation-suite",
                        "configuration_overrides": run_time_overrides,
                    },
                },
            },
        )
        return body["data"]["attributes"] | {"id": body["data"]["id"]}

    async def get_run(self, run_id: str) -> dict:
        body = await self._request("GET", f"{self._admin_base}/runs/{run_id}")
        return body["data"]["attributes"] | {"id": body["data"]["id"]}

    async def update_run(self, run_id: str, status: str, **fields) -> dict:
        body = await self._request(
            "PATCH", f"{self._admin_base}/runs/{run_id}",
            json={"data": {"attributes": {"status": status, **fields}}},
        )
        return body["data"]["attributes"] | {"id": body["data"]["id"]}

    async def search(self, query: str, answers_so_far: list[dict], run_time_overrides: dict) -> dict:
        return await self._request(
            "POST", f"{self._admin_base}/searches",
            json={"q": query, "answers": answers_so_far, "configuration_overrides": run_time_overrides},
        )

    async def post_result(self, result: dict) -> dict:
        body = await self._request("POST", f"{self._admin_base}/results", json={"data": [result]})
        item = body["data"][0]
        return item["attributes"] | {"id": item["id"]}

    async def aclose(self):
        await self._http.aclose()
