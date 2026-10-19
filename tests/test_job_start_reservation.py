"""Two concurrent job starts cannot both pass the running-jobs cap.

Synchronous POST /api/jobs handlers run on worker threads, so a spawn
slowdown let two requests both pass the capacity check before either
registered its process. The reservation lock around check/spawn/register
keeps the cap honest; a start that fails after spawning releases its slot.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "classification-evals"))

from fastapi import HTTPException

from backend import app


class FakePopen:
    def __init__(self, *args, spawn_delay: float = 0.0, **kwargs):
        if spawn_delay:
            # Stand in for real spawn latency: the window in which a second
            # request used to pass the capacity check before this one
            # registered.
            time.sleep(spawn_delay)
        self.pid = FakePopen._next_pid()
        self._killed = False

    _pid_counter = 0
    _pid_lock = threading.Lock()

    @classmethod
    def _next_pid(cls) -> int:
        with cls._pid_lock:
            cls._pid_counter += 1
            return cls._pid_counter

    def poll(self):
        return None

    def kill(self):
        self._killed = True

    def wait(self, timeout=None):
        return 0


class JobStartReservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state_root = root
        self._saved_state = (app.STATE_DIR, app.JOBS_DIR, app.DB_PATH)
        app.STATE_DIR = root
        app.JOBS_DIR = root / "jobs"
        app.DB_PATH = root / "jobs.sqlite"
        app._init_db()
        self._saved_processes = dict(app._PROCESSES)
        app._PROCESSES.clear()
        self._saved_watch = app._watch_process
        app._watch_process = lambda *args, **kwargs: None
        self._saved_key = app._openai_api_key
        app._openai_api_key = lambda: "test-key"
        self.patches = [
            mock.patch("subprocess.Popen", FakePopen),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        app._openai_api_key = self._saved_key
        app._watch_process = self._saved_watch
        app._PROCESSES.clear()
        app._PROCESSES.update(self._saved_processes)
        app.STATE_DIR, app.JOBS_DIR, app.DB_PATH = self._saved_state

    def _req(self, **overrides):
        from backend.app import JobCreate

        payload = {"allow_spend": True, "run_label": "reservation_test"}
        payload.update(overrides)
        return JobCreate(**payload)

    def test_concurrent_starts_respect_cap_one(self):
        # Default cap is CLASSIFY_EVAL_MAX_RUNNING_JOBS=1 and the default
        # request is valid (1 persona, limit 5 -> 5 sessions, well under cap).
        results = []
        errors = []

        def start():
            try:
                results.append(app.create_job(self._req()))
            except HTTPException as exc:
                errors.append(exc)

        spawn_delay = 0.25
        with mock.patch("subprocess.Popen", lambda *a, **k: FakePopen(*a, spawn_delay=spawn_delay, **k)):
            threads = [threading.Thread(target=start) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(results), 1, f"expected exactly one accepted start, got {len(results)}")
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status_code, 429)
        self.assertIn("active job", errors[0].detail)
        self.assertEqual(app._running_process_count(), 1)

    def test_sequential_second_start_rejected_while_first_runs(self):
        first = app.create_job(self._req())
        self.assertEqual(first["status"], "running")
        with self.assertRaises(HTTPException) as caught:
            app.create_job(self._req())
        self.assertEqual(caught.exception.status_code, 429)

    def test_failed_start_releases_reserved_slot(self):
        real_connect = app._connect

        def failing_connect():
            raise sqlite3.OperationalError("jobs database unavailable")

        process = FakePopen()
        process.wait = mock.Mock(return_value=0)
        app._connect = failing_connect
        try:
            with mock.patch("subprocess.Popen", return_value=process):
                with self.assertRaises(sqlite3.OperationalError):
                    app.create_job(self._req())
        finally:
            app._connect = real_connect

        self.assertTrue(process._killed)
        process.wait.assert_called_once_with()
        self.assertEqual(app._running_process_count(), 0)
        self.assertEqual(app._PROCESSES, {})
        with app._connect() as conn:
            rows = conn.execute("SELECT id FROM jobs").fetchall()
        self.assertEqual(rows, [])

        # The reserved slot is free: a later start succeeds.
        second = app.create_job(self._req())
        self.assertEqual(second["status"], "running")

    def test_cap_one_accepts_two_sequential_complete_starts(self):
        first = app.create_job(self._req())
        # The first job exits, so its slot is released and a second start
        # is accepted even though the cap is one.
        job_id = first["job_id"]
        process = app._PROCESSES[job_id]
        app._PROCESSES.pop(job_id)
        app._record_exit(job_id, 0)
        self.assertFalse(process._killed)
        second = app.create_job(self._req())
        self.assertEqual(second["status"], "running")


if __name__ == "__main__":
    unittest.main()
