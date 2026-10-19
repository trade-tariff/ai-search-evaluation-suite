"""Exports resolve the selected run and do not change saved snapshots."""

import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/product/backend"))

from fastapi.testclient import TestClient
import main
from judge import evaluate_pair
from schemas import AppConfig, BenchmarkRun, CompletionResult


def fixture(run_id):
    result = CompletionResult(model_id=f"{run_id}-model", prompt_index=1,
                              response_text='{"answers":[{"commodity_code":"6404199000"}]}')
    return BenchmarkRun(id=run_id, timestamp="fixture", model_results=[result],
                        evaluations=[evaluate_pair(result, result)])


class BenchmarkExportTest(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.old = fixture("old-run")
        self.current = fixture("new-run")
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.saved_file = directory / "benchmark_old-run.json"
        self.saved_file.write_text(self.old.model_dump_json())
        self.saved_bytes = self.saved_file.read_bytes()
        self.saved_mtime = self.saved_file.stat().st_mtime_ns
        stack.enter_context(patch.object(main, "RESULTS_DIR", directory))
        stack.enter_context(patch.object(main, "get_current_run", return_value=self.current))
        self.load = stack.enter_context(patch.object(main, "load_saved_run", side_effect=lambda key: self.old if key == "old-run" else None))
        stack.enter_context(patch.object(main, "load_config", return_value=AppConfig()))
        self.client = stack.enter_context(TestClient(main.app))

    def test_selected_json_and_csv_export_the_historical_run(self):
        response = self.client.get("/api/benchmark/export/json?run_id=old-run")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], "old-run")
        self.assertIn("benchmark_old-run.json", response.headers["content-disposition"])
        response = self.client.get("/api/benchmark/export/csv?run_id=old-run")
        self.assertEqual(response.status_code, 200)
        self.assertIn("old-run-model", response.text)
        self.assertNotIn("new-run-model", response.text)
        self.assertIn("benchmark_old-run.csv", response.headers["content-disposition"])
        self.assertEqual(self.saved_file.read_bytes(), self.saved_bytes)
        self.assertEqual(self.saved_file.stat().st_mtime_ns, self.saved_mtime)

    def test_default_and_current_keep_existing_export_behavior(self):
        for query in ("", "?run_id=current"):
            response = self.client.get(f"/api/benchmark/export/json{query}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["id"], "new-run")
        self.load.assert_not_called()

    def test_unknown_run_does_not_fall_back_to_current(self):
        for kind in ("json", "csv"):
            response = self.client.get(f"/api/benchmark/export/{kind}?run_id=missing")
            self.assertEqual(response.status_code, 404)

    def test_invalid_id_is_rejected_before_file_lookup(self):
        response = self.client.get("/api/benchmark/export/json", params={"run_id": "../outside"})
        self.assertEqual(response.status_code, 422)
        self.load.assert_not_called()
