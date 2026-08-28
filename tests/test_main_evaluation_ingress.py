import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from fastapi.testclient import TestClient

import main


class EvaluationIngressTest(unittest.TestCase):
    def setUp(self):
        main._active_evaluation_runs.clear()  # module-level guard set — reset between tests
        main._background_evaluation_tasks.clear()
        self.client = TestClient(main.app)

    def test_starts_a_run_and_returns_202(self):
        with patch("main.execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})):
            response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"started": True, "run_id": "107"})

    def test_refuses_a_second_concurrent_start_of_the_same_run_id(self):
        main._active_evaluation_runs.add("107")

        response = self.client.post("/api/evaluation/runs/107/start")

        self.assertEqual(response.status_code, 409)

    def test_a_different_run_id_is_not_blocked_by_another_runs_guard(self):
        main._active_evaluation_runs.add("107")

        with patch("main.execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})):
            response = self.client.post("/api/evaluation/runs/108/start")

        self.assertEqual(response.status_code, 202)


class EvaluationIngressGuardReleaseTest(unittest.IsolatedAsyncioTestCase):
    """Calls the route function directly (bypassing HTTP/TestClient) so the
    background task it schedules runs on THIS test's own event loop, and can
    be awaited directly — proving _active_evaluation_runs actually clears
    once the background work finishes, success or failure, rather than just
    trusting the try/finally nesting by inspection. This is also what proves
    asyncio.create_task's result is being kept alive: if the task reference
    were dropped instead of stored in _background_evaluation_tasks, there
    would be nothing here to await."""

    def setUp(self):
        main._active_evaluation_runs.clear()
        main._background_evaluation_tasks.clear()

    async def test_guard_is_released_once_the_background_task_completes(self):
        # `await task` must stay INSIDE this `with patch(...)` block.
        # asyncio.create_task() only *schedules* the coroutine — it does not
        # run any of its body until something actually yields control back
        # to the event loop. api_start_evaluation_run has no await after
        # creating the task, so it returns before the task has executed a
        # single line. If the patch were closed before `await task`, the
        # task's `execute_run(...)` call would run against the real
        # (unpatched) function once awaited here, hitting the real network.
        with patch("main.execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})):
            response = await main.api_start_evaluation_run(run_id="107")

            self.assertEqual(len(main._background_evaluation_tasks), 1)
            task = next(iter(main._background_evaluation_tasks))

            await task  # let the background work actually finish, patch still active

        self.assertEqual(response, {"started": True, "run_id": "107"})
        self.assertNotIn("107", main._active_evaluation_runs)
        self.assertEqual(main._background_evaluation_tasks, set())

    async def test_guard_is_released_even_if_execute_run_raises(self):
        # Same reasoning as above: `await task` must stay inside the patch
        # context, or the background task runs the real execute_run once
        # awaited outside it.
        with patch("main.execute_run", new=AsyncMock(side_effect=RuntimeError("boom"))):
            await main.api_start_evaluation_run(run_id="108")

            task = next(iter(main._background_evaluation_tasks))
            with self.assertRaises(RuntimeError):
                await task

        self.assertNotIn("108", main._active_evaluation_runs)
        self.assertEqual(main._background_evaluation_tasks, set())


if __name__ == "__main__":
    unittest.main()
