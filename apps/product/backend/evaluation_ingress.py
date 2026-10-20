"""Backend-driven evaluation ingress for the worker."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException

from classification_core.trade_tariff_backend.client import TradeTariffBackendClient
from classification_core.trade_tariff_backend.execute_run import execute_run

router = APIRouter()

# Keep the duplicate-run guard and strong task references shared by both apps.
_active_evaluation_runs: set[str] = set()
_background_evaluation_tasks: set[asyncio.Task] = set()


@router.post("/api/evaluation/runs/{run_id}/start", status_code=202)
async def api_start_evaluation_run(run_id: str):
    if run_id in _active_evaluation_runs:
        raise HTTPException(409, f"Run {run_id} is already being executed.")

    _active_evaluation_runs.add(run_id)

    async def _run_and_release():
        try:
            client = TradeTariffBackendClient()
            try:
                await execute_run(run_id, client)
            finally:
                await client.aclose()
        finally:
            _active_evaluation_runs.discard(run_id)

    task = asyncio.create_task(_run_and_release())
    _background_evaluation_tasks.add(task)
    task.add_done_callback(_background_evaluation_tasks.discard)
    logging.getLogger("experiment").info(
        "experiment run accepted run_id=%s",
        run_id,
        extra={"event": "experiment_run_started", "run_id": run_id},
    )
    return {"started": True, "run_id": run_id}
