import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from classification_core.trade_tariff_backend.cli import create_and_run


class CreateAndRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_creates_an_experiment_then_a_run_then_executes_it_with_a_fresh_idempotency_key(self):
        fake_client = AsyncMock()
        fake_client.create_experiment.return_value = {"id": "42", "name": "local-test"}
        fake_client.create_run.return_value = {"id": "107"}

        with patch("classification_core.trade_tariff_backend.cli.TradeTariffBackendClient", return_value=fake_client), \
             patch("classification_core.trade_tariff_backend.cli.execute_run", new=AsyncMock(return_value={"status": "completed", "succeeded": 1, "failed": 0})) as mocked_execute:
            summary = await create_and_run(experiment_name="local-test", run_time_overrides={"max_rounds": 2})

        fake_client.create_experiment.assert_awaited_once_with("local-test")
        create_run_kwargs = fake_client.create_run.call_args.kwargs
        self.assertEqual(create_run_kwargs["experiment_id"], "42")
        self.assertEqual(create_run_kwargs["run_time_overrides"], {"max_rounds": 2})
        self.assertTrue(create_run_kwargs["idempotency_key"])  # a real UUID was generated, not blank
        mocked_execute.assert_awaited_once_with("107", fake_client)
        self.assertEqual(summary, {"status": "completed", "succeeded": 1, "failed": 0})


if __name__ == "__main__":
    unittest.main()
