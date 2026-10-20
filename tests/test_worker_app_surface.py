"""Check the worker application surface and ingress."""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from worker.app import app as worker_app

import worker.ingress as ingress

class WorkerAppSurfaceTest(unittest.TestCase):
    def setUp(self):
        ingress._active_evaluation_runs.clear()
        ingress._background_evaluation_tasks.clear()
        self.client = TestClient(worker_app)

    def test_evaluation_start_route_is_reachable_on_the_real_worker_app(self):
        with patch.object(ingress, "execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})):
            response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"started": True, "run_id": "107"})

    def test_ui_routes_and_assets_are_not_exposed(self):
        for path in ("/", "/workbench", "/workbench/", "/workbench/example", "/assets/example.js"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_research_api_paths_are_not_exposed(self):
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

    def test_workbench_api_path_is_not_exposed(self):
        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 404)

    def test_evaluation_start_route_keeps_the_duplicate_run_guard(self):
        ingress._active_evaluation_runs.add("107")

        response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "Run 107 is already being executed."})

if __name__ == "__main__":
    unittest.main()
