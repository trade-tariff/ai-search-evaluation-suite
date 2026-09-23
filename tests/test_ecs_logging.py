"""Structured logging matches the backend Logstash shape and hides health probes."""

from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "classification-evals"))

from backend.logging_config import DropHealthCheckFilter, EcsJsonFormatter, is_healthcheck_path


class HealthCheckFilterTest(unittest.TestCase):
    def setUp(self):
        self.filter = DropHealthCheckFilter()

    def test_known_probe_paths(self):
        self.assertTrue(is_healthcheck_path("/api/health"))
        self.assertTrue(is_healthcheck_path("/api/health?ready=1"))
        self.assertTrue(is_healthcheck_path("/api/live"))
        self.assertFalse(is_healthcheck_path("/api/jobs"))

    def test_uvicorn_access_record_for_health_is_dropped(self):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/api/health", "1.1", 200),
            None,
        )
        self.assertFalse(self.filter.filter(record))

    def test_application_request_is_kept(self):
        record = logging.LogRecord(
            "ecs.access",
            logging.INFO,
            __file__,
            1,
            "[200] GET /api/jobs",
            (),
            None,
        )
        record.path = "/api/jobs"
        self.assertTrue(self.filter.filter(record))


class EcsJsonFormatterTest(unittest.TestCase):
    def test_logstash_fields_for_an_experiment_run(self):
        record = logging.LogRecord(
            "experiment",
            logging.INFO,
            __file__,
            1,
            "experiment run started job_id=abc",
            (),
            None,
        )
        record.event = "experiment_run_started"
        record.job_id = "abc"
        record.run_label = "demo"
        payload = json.loads(EcsJsonFormatter().format(record))
        self.assertEqual(payload["@version"], "1")
        self.assertIn("T", payload["@timestamp"])
        self.assertEqual(payload["message"], "experiment run started job_id=abc")
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["event"], "experiment_run_started")
        self.assertEqual(payload["job_id"], "abc")
        self.assertEqual(payload["run_label"], "demo")

    def test_uvicorn_access_record_uses_backend_request_fields(self):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:9", "POST", "/api/jobs?x=1", "1.1", 202),
            None,
        )
        payload = json.loads(EcsJsonFormatter().format(record))
        self.assertEqual(payload["method"], "POST")
        self.assertEqual(payload["path"], "/api/jobs")
        self.assertEqual(payload["status"], 202)
        self.assertEqual(payload["message"], "[202] POST /api/jobs")


if __name__ == "__main__":
    unittest.main()
