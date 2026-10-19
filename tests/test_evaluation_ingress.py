"""Exercise the worker router without importing the research workbench."""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from fastapi import FastAPI
from fastapi.testclient import TestClient

import evaluation_ingress as ingress

unit_app = FastAPI()
unit_app.include_router(ingress.router)


class EvaluationIngressTest(unittest.TestCase):
    def setUp(self):
        ingress._active_evaluation_runs.clear()
        ingress._background_evaluation_tasks.clear()
        self.client = TestClient(unit_app)

    def test_starts_a_run_and_returns_202(self):
        with patch.object(ingress, "execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})):
            response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"started": True, "run_id": "107"})

    def test_refuses_a_second_concurrent_start_of_the_same_run_id(self):
        ingress._active_evaluation_runs.add("107")

        response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 409)

    def test_a_different_run_id_is_not_blocked_by_another_runs_guard(self):
        ingress._active_evaluation_runs.add("107")

        with patch.object(ingress, "execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})):
            response = self.client.post("/api/evaluation/runs/108/start")

        self.assertEqual(response.status_code, 202)


class EvaluationIngressGuardReleaseTest(unittest.IsolatedAsyncioTestCase):
    """Await the retained background task to check guard and client cleanup."""

    def setUp(self):
        ingress._active_evaluation_runs.clear()
        ingress._background_evaluation_tasks.clear()

    async def test_guard_is_released_once_the_background_task_completes(self):
        backend = AsyncMock()
        # Keep both patches active until the scheduled task has finished.
        with (
            patch.object(ingress, "TradeTariffBackendClient", return_value=backend),
            patch.object(ingress, "execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})),
        ):
            response = await ingress.api_start_evaluation_run(run_id="107")
            self.assertEqual(len(ingress._background_evaluation_tasks), 1)
            task = next(iter(ingress._background_evaluation_tasks))
            await task

        self.assertEqual(response, {"started": True, "run_id": "107"})
        self.assertNotIn("107", ingress._active_evaluation_runs)
        self.assertEqual(ingress._background_evaluation_tasks, set())
        backend.aclose.assert_awaited_once()

    async def test_guard_is_released_even_if_execute_run_raises(self):
        backend = AsyncMock()
        with (
            patch.object(ingress, "TradeTariffBackendClient", return_value=backend),
            patch.object(ingress, "execute_run", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            await ingress.api_start_evaluation_run(run_id="108")
            task = next(iter(ingress._background_evaluation_tasks))
            with self.assertRaises(RuntimeError):
                await task

        self.assertNotIn("108", ingress._active_evaluation_runs)
        self.assertEqual(ingress._background_evaluation_tasks, set())
        backend.aclose.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
