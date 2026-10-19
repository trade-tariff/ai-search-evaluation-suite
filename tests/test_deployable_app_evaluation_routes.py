"""Check the worker ingress on the actual deployable app."""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "classification-evals"))

from fastapi.testclient import TestClient

from backend.app import app as deployable_app

import evaluation_ingress as ingress


class DeployableAppEvaluationRoutesTest(unittest.TestCase):
    def setUp(self):
        ingress._active_evaluation_runs.clear()
        ingress._background_evaluation_tasks.clear()
        self.client = TestClient(deployable_app)

    def test_evaluation_start_route_is_reachable_on_the_real_deployable_app(self):
        with patch.object(ingress, "execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})):
            response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"started": True, "run_id": "107"})

    def test_evaluation_start_route_keeps_the_duplicate_run_guard(self):
        ingress._active_evaluation_runs.add("107")

        response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Run 107 is already being executed."})


if __name__ == "__main__":
    unittest.main()
