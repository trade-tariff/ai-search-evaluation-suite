import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

import httpx

from classification_core.trade_tariff_backend.client import TradeTariffBackendClient
from classification_core_trade_tariff_backend_fixtures import EXPERIMENT_CREATE_RESPONSE, RUN_CREATE_RESPONSE, RUN_UPDATE_RESPONSE


def _client_with_transport(handler):
    return TradeTariffBackendClient(base_url="http://backend.test", transport=httpx.MockTransport(handler))


class CreateExperimentTest(unittest.IsolatedAsyncioTestCase):
    async def test_posts_the_name_and_returns_id_plus_attributes(self):
        def handler(request):
            self.assertEqual(request.url.path, "/uk/admin/search/evaluation/experiments")
            body = httpx.Request("POST", request.url, content=request.content).content
            import json
            payload = json.loads(body)
            self.assertEqual(payload["data"]["attributes"]["name"], "ai1073-test-experiment")
            return httpx.Response(201, json=EXPERIMENT_CREATE_RESPONSE)

        client = _client_with_transport(handler)
        result = await client.create_experiment("ai1073-test-experiment")

        self.assertEqual(result["id"], "42")
        self.assertEqual(result["name"], "ai1073-test-experiment")


class CreateRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_sends_the_idempotency_key_header_and_returns_run_id(self):
        seen_keys = []

        def handler(request):
            self.assertEqual(request.url.path, "/uk/admin/search/evaluation/runs")
            seen_keys.append(request.headers.get("Idempotency-Key"))
            return httpx.Response(201, json=RUN_CREATE_RESPONSE)

        client = _client_with_transport(handler)
        result = await client.create_run(
            experiment_id="42", run_time_overrides={"max_rounds": 3}, idempotency_key="fixed-key-123",
        )

        self.assertEqual(result["id"], "107")
        self.assertEqual(seen_keys, ["fixed-key-123"])

    async def test_resends_the_identical_idempotency_key_on_each_low_level_retry_attempt(self):
        seen_keys = []

        def handler(request):
            seen_keys.append(request.headers.get("Idempotency-Key"))
            if len(seen_keys) == 1:
                return httpx.Response(503, json={"errors": [{"detail": "unavailable"}]})
            return httpx.Response(201, json=RUN_CREATE_RESPONSE)

        client = _client_with_transport(handler)
        await client.create_run(experiment_id="42", run_time_overrides={}, idempotency_key="fixed-key-123")

        self.assertEqual(seen_keys, ["fixed-key-123", "fixed-key-123"])


class GetRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_a_run_by_id_including_effective_configuration(self):
        def handler(request):
            self.assertEqual(request.url.path, "/uk/admin/search/evaluation/runs/107")
            return httpx.Response(200, json=RUN_CREATE_RESPONSE)

        client = _client_with_transport(handler)
        result = await client.get_run("107")

        self.assertEqual(result["id"], "107")
        self.assertEqual(result["effective_configuration"]["question_model"], "gpt-5-mini-2025-08-07")


class UpdateRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_patches_status_and_extra_fields(self):
        def handler(request):
            self.assertEqual(request.url.path, "/uk/admin/search/evaluation/runs/107")
            self.assertEqual(request.method, "PATCH")
            return httpx.Response(200, json=RUN_UPDATE_RESPONSE)

        client = _client_with_transport(handler)
        result = await client.update_run("107", status="running")

        self.assertEqual(result["status"], "running")


if __name__ == "__main__":
    unittest.main()
