import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from classification_core.trade_tariff_backend.cli import create_and_run, main


class CreateAndRunTest(unittest.IsolatedAsyncioTestCase):
    def fake_client(self, *run_statuses):
        client = AsyncMock()
        client.create_experiment.return_value = {"id": "42", "name": "local-test"}
        client.create_run.return_value = {"id": "107"}
        client.get_run.side_effect = [{"id": "107", **status} for status in run_statuses]
        return client

    async def test_creates_an_experiment_then_a_run_with_a_fresh_idempotency_key(self):
        client = self.fake_client({"status": "completed", "result_count": 3, "error_count": 0, "error_summary": None})

        with patch("classification_core.trade_tariff_backend.cli.TradeTariffBackendClient", return_value=client):
            await create_and_run(experiment_name="local-test", run_time_overrides={"max_rounds": 2}, gold_query_set_id=5, poll_interval_seconds=0)

        # The experiment is created pointing at the gold query set, because a run for an
        # experiment without one fails as soon as it starts.
        client.create_experiment.assert_awaited_once_with("local-test", 5)
        create_run_kwargs = client.create_run.call_args.kwargs
        self.assertEqual(create_run_kwargs["experiment_id"], "42")
        self.assertEqual(create_run_kwargs["run_time_overrides"], {"max_rounds": 2})
        self.assertTrue(create_run_kwargs["idempotency_key"])  # a real UUID was generated, not blank

    async def test_follows_the_run_until_it_finishes_rather_than_executing_it_locally(self):
        client = self.fake_client(
            {"status": "queued", "result_count": 0, "error_count": 0, "error_summary": None},
            {"status": "running", "result_count": 1, "error_count": 0, "error_summary": None},
            {"status": "completed", "result_count": 3, "error_count": 0, "error_summary": None},
        )

        with patch("classification_core.trade_tariff_backend.cli.TradeTariffBackendClient", return_value=client):
            summary = await create_and_run(experiment_name="local-test", run_time_overrides={}, gold_query_set_id=5, poll_interval_seconds=0)

        self.assertEqual(client.get_run.await_count, 3)
        client.get_run.assert_awaited_with("107")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["result_count"], 3)

    async def test_reports_why_a_run_failed_to_start(self):
        client = self.fake_client(
            {"status": "failed", "result_count": 0, "error_count": 0, "error_summary": "could not reach the eval app: connection refused"},
        )

        with patch("classification_core.trade_tariff_backend.cli.TradeTariffBackendClient", return_value=client):
            summary = await create_and_run(experiment_name="local-test", run_time_overrides={}, gold_query_set_id=5, poll_interval_seconds=0)

        self.assertEqual(summary["status"], "failed")
        self.assertIn("could not reach the eval app", summary["error_summary"])


class MainTest(unittest.TestCase):
    def test_passes_the_gold_query_set_option_through_as_a_number(self):
        with patch.object(sys, "argv", ["cli", "local-test", "--gold-query-set", "5"]), \
             patch("classification_core.trade_tariff_backend.cli.create_and_run", new=AsyncMock(return_value={"status": "completed"})) as mocked, \
             contextlib.redirect_stdout(io.StringIO()):
            main()

        mocked.assert_awaited_once_with("local-test", run_time_overrides={}, gold_query_set_id=5)

    def test_refuses_to_start_without_a_gold_query_set(self):
        with patch.object(sys, "argv", ["cli", "local-test"]), \
             patch("classification_core.trade_tariff_backend.cli.create_and_run", new=AsyncMock()) as mocked, \
             contextlib.redirect_stderr(io.StringIO()) as stderr, \
             self.assertRaises(SystemExit):
            main()

        mocked.assert_not_awaited()
        self.assertIn("--gold-query-set", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
