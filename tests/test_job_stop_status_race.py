"""A requested stop records an honest terminal status, never stuck stopping.

stop_job used to send SIGTERM first and then write status='stopping'
unconditionally. When the process died from the signal before that write,
the watcher's terminal status was overwritten and the row stayed stopping
forever (_refresh_job only finalised running rows). Stop intent is now
recorded before the signal, every transition is conditional, and a
requested SIGTERM exit is cancelled rather than failed.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "classification-evals"))

from backend import app


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid

    def poll(self):
        return None


class StopStatusRaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._saved_state = (app.STATE_DIR, app.JOBS_DIR, app.DB_PATH)
        app.STATE_DIR = root
        app.JOBS_DIR = root / "jobs"
        app.DB_PATH = root / "jobs.sqlite"
        app._init_db()
        self._saved_processes = dict(app._PROCESSES)
        app._PROCESSES.clear()
        self._saved_grace = os.environ.get("CLASSIFY_EVAL_STOP_GRACE_SECONDS")
        os.environ["CLASSIFY_EVAL_STOP_GRACE_SECONDS"] = "0"

    def tearDown(self):
        app._PROCESSES.clear()
        app._PROCESSES.update(self._saved_processes)
        app.STATE_DIR, app.JOBS_DIR, app.DB_PATH = self._saved_state
        if self._saved_grace is None:
            os.environ.pop("CLASSIFY_EVAL_STOP_GRACE_SECONDS", None)
        else:
            os.environ["CLASSIFY_EVAL_STOP_GRACE_SECONDS"] = self._saved_grace

    def _insert_job(self, job_id: str, status: str, pid: int = 4321, returncode=None) -> None:
        with app._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs
                  (id, created_at, updated_at, status, pid, returncode, command_json,
                   request_json, log_path, error)
                VALUES (?, 0, 0, ?, ?, ?, '[]', ?, 'log', NULL)
                """,
                (job_id, status, pid, returncode, json.dumps({"run_label": "stop-race", "harness": "classify"})),
            )

    def _status(self, job_id: str) -> str:
        with app._connect() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row["status"]

    def test_exit_from_requested_sigterm_is_cancelled_not_stuck_or_failed(self):
        # Reproduce the race: the process dies from SIGTERM the instant it is
        # signalled, so the watcher records the terminal status while stop_job
        # is still between the signal and its status write.
        self._insert_job("job-1", "running")
        app._PROCESSES["job-1"] = FakeProcess(4321)

        def killpg_terminates(pid: int, sig: int) -> None:
            assert sig == signal.SIGTERM
            # The watcher observes the exit before stop_job returns.
            app._record_exit("job-1", -signal.SIGTERM)

        with mock.patch("os.killpg", side_effect=killpg_terminates):
            job = app.stop_job("job-1")

        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(self._status("job-1"), "cancelled")
        self.assertNotEqual(self._status("job-1"), "stopping")

    def test_stop_intent_is_recorded_before_the_signal(self):
        self._insert_job("job-2", "running")
        app._PROCESSES["job-2"] = FakeProcess(4322)
        observed = {}

        def killpg_checks_intent(pid: int, sig: int) -> None:
            observed["status_at_signal"] = self._status("job-2")

        with mock.patch("os.killpg", side_effect=killpg_checks_intent):
            app.stop_job("job-2")

        self.assertEqual(observed.get("status_at_signal"), "stopping")

    def test_stop_does_not_overwrite_terminal_status(self):
        # The watcher recorded the exit before stop_job claimed the intent:
        # the conditional claim is a no-op and the terminal status survives.
        self._insert_job("job-3", "failed", returncode=1)
        app._PROCESSES["job-3"] = FakeProcess(4323)
        with mock.patch("os.killpg") as killpg:
            job = app.stop_job("job-3")
        killpg.assert_not_called()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self._status("job-3"), "failed")

    def test_stop_claim_once_then_second_stop_is_harmless(self):
        self._insert_job("job-4", "running")
        app._PROCESSES["job-4"] = FakeProcess(4324)
        signals = []

        with mock.patch("os.killpg", side_effect=lambda pid, sig: signals.append(sig)):
            first = app.stop_job("job-4")
            second = app.stop_job("job-4")
        self.assertEqual(first["status"], "stopping")
        self.assertEqual(second["status"], "stopping")
        # The second stop found the intent already claimed and did not signal.
        self.assertEqual(signals, [signal.SIGTERM])

    def test_stopping_row_is_finalised_by_refresh_when_process_exited(self):
        # A stopping row whose local process handle shows an exit is finalised
        # by a read, so a lost watcher cannot strand the row either.
        self._insert_job("job-5", "stopping")
        exited = FakeProcess(4325)
        exited.poll = lambda: -signal.SIGTERM
        app._PROCESSES["job-5"] = exited
        with app._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id='job-5'").fetchone()
        job = app._refresh_job(row)
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(self._status("job-5"), "cancelled")

    def test_escalation_kills_a_job_that_ignores_sigterm(self):
        os.environ["CLASSIFY_EVAL_STOP_GRACE_SECONDS"] = "0.05"
        self._insert_job("job-6", "running")
        app._PROCESSES["job-6"] = FakeProcess(4326)
        kills = []
        escalated = threading.Event()

        def killpg(pid: int, sig: int) -> None:
            kills.append(sig)
            if sig == signal.SIGKILL:
                escalated.set()

        with mock.patch("os.killpg", side_effect=killpg):
            app.stop_job("job-6")
            self.assertTrue(escalated.wait(timeout=5), "escalation to SIGKILL never fired")

        self.assertEqual(kills[0], signal.SIGTERM)
        self.assertIn(signal.SIGKILL, kills)
        # The escalated exit is still a requested stop, not a failure.
        app._record_exit("job-6", -signal.SIGKILL)
        self.assertEqual(self._status("job-6"), "cancelled")

    def test_unrequested_exit_statuses_are_unchanged(self):
        self._insert_job("job-7", "running")
        app._record_exit("job-7", 0)
        self.assertEqual(self._status("job-7"), "succeeded")
        self._insert_job("job-8", "running")
        app._record_exit("job-8", 3)
        self.assertEqual(self._status("job-8"), "failed")
        self._insert_job("job-9", "running")
        app._record_exit("job-9", -1, status="unknown_exit")
        self.assertEqual(self._status("job-9"), "unknown_exit")

if __name__ == "__main__":
    unittest.main()
