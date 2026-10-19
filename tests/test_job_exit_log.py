"""A job exit writes one finish line even when two callers record it."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "classification-evals"))

from backend import app


class RecordExitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        app.STATE_DIR = root
        app.JOBS_DIR = root / "jobs"
        app.DB_PATH = root / "jobs.sqlite"
        app._init_db()
        with app._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs
                  (id, created_at, updated_at, status, pid, returncode, command_json,
                   request_json, log_path, error)
                VALUES ('job-1', 0, 0, 'running', 1, NULL, '[]', ?, 'log', NULL)
                """,
                (json.dumps({"run_label": "trial", "harness": "qa"}),),
            )

    def test_second_exit_does_not_log_again(self):
        with self.assertLogs("experiment", level="INFO") as captured:
            app._record_exit("job-1", 0)
            app._record_exit("job-1", -1, status="unknown_exit")
        finished = [line for line in captured.output if "experiment run finished" in line]
        self.assertEqual(finished, [
            "INFO:experiment:experiment run finished job_id=job-1 status=succeeded returncode=0",
        ])

    def test_concurrent_exits_log_once(self):
        barrier = threading.Barrier(2)

        def record(returncode: int, status: str | None) -> None:
            barrier.wait()
            app._record_exit("job-1", returncode, status=status)

        with self.assertLogs("experiment", level="INFO") as captured:
            threads = [
                threading.Thread(target=record, args=(0, None)),
                threading.Thread(target=record, args=(-1, "unknown_exit")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finished = [line for line in captured.output if "experiment run finished" in line]
        self.assertEqual(len(finished), 1)
