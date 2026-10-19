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

    def test_native_research_apis_are_not_exposed(self):
        for method, path in (
            ("GET", "/api/jobs"),
            ("POST", "/api/evals/classification/trial"),
            ("GET", "/eval/matrix"),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 404)

    def test_health_endpoints_remain_available(self):
        for path in ("/api/health", "/api/live"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

    def test_research_entrypoint_does_not_add_its_routes_to_worker(self):
        from backend.research_app import app as research_app

        self.assertIsNot(research_app, deployable_app)
        research_paths = research_app.openapi()["paths"]
        worker_paths = deployable_app.openapi()["paths"]
        for path in ("/api/config", "/api/jobs", "/api/evals/classification/trial", "/eval/matrix"):
            with self.subTest(path=path):
                self.assertIn(path, research_paths)
                self.assertNotIn(path, worker_paths)

    def test_copied_workbench_api_is_not_exposed(self):
        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 404)

    def test_evaluation_start_route_keeps_the_duplicate_run_guard(self):
        ingress._active_evaluation_runs.add("107")

        response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Run 107 is already being executed."})


if __name__ == "__main__":
    unittest.main()
