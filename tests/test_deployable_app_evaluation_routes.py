"""main.py's own app object is never run as a server by itself — the only
FastAPI app actually bound to a port, locally or in the Docker image, is
apps/classification-evals/backend/app.py's app, which copies routes in from
main.py filtered through _DEPLOYABLE_WORKBENCH_PREFIXES/_EXACT_PATHS
(app.py:1822-1884). test_main_evaluation_ingress.py alone would NOT have
caught /api/evaluation/ being missing from that allowlist, because it tests
main.app directly rather than the app that's actually deployed. This test
imports the real deployable app the same way ./start.sh does, to close
that gap."""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "classification-evals"))

from fastapi.testclient import TestClient

from backend.app import app as deployable_app

import main


class DeployableAppEvaluationRoutesTest(unittest.TestCase):
    def setUp(self):
        main._active_evaluation_runs.clear()
        main._background_evaluation_tasks.clear()
        self.client = TestClient(deployable_app)

    def test_evaluation_start_route_is_reachable_on_the_real_deployable_app(self):
        with patch("main.execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})):
            response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"started": True, "run_id": "107"})


if __name__ == "__main__":
    unittest.main()
