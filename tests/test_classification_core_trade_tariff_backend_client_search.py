import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

import httpx

from classification_core.trade_tariff_backend.client import (
    TradeTariffBackendClient,
    TradeTariffBackendUnavailableError,
    TradeTariffBackendValidationError,
)
from classification_core_trade_tariff_backend_fixtures import SEARCH_RESPONSE_CONVERGED


def _polling_client(handler, **kwargs):
    kwargs.setdefault("poll_interval_s", 0.01)
    return TradeTariffBackendClient(base_url="http://backend.test", transport=httpx.MockTransport(handler), **kwargs)


class SearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_posts_query_answers_so_far_and_overrides_then_polls_until_complete(self):
        seen_payload = {}
        polls = {"n": 0}

        def handler(request):
            if request.method == "POST":
                self.assertEqual(request.url.path, "/uk/admin/search/evaluation/searches")
                seen_payload.update(json.loads(request.content))
                return httpx.Response(202, json={"id": "search-1", "status": "queued"})

            self.assertEqual(request.url.path, "/uk/admin/search/evaluation/searches/search-1")
            polls["n"] += 1
            if polls["n"] < 2:
                return httpx.Response(200, json={"id": "search-1", "status": "running"})
            return httpx.Response(200, json={"id": "search-1", "status": "completed", "response_status": 200, "result": SEARCH_RESPONSE_CONVERGED})

        client = _polling_client(handler)
        answers_so_far = [{"question": "What are the uppers made of?", "answer": "Textile", "options": ["Leather", "Textile"]}]

        result = await client.search(
            query="women's trainers", answers_so_far=answers_so_far, run_time_overrides={"max_rounds": 3},
            request_id="00000000-0000-0000-0000-000000000000",
        )

        self.assertEqual(seen_payload["q"], "women's trainers")
        self.assertEqual(seen_payload["answers"], answers_so_far)
        self.assertEqual(seen_payload["configuration_overrides"], {"max_rounds": 3})
        self.assertEqual(result, SEARCH_RESPONSE_CONVERGED)
        self.assertEqual(polls["n"], 2)

    async def test_sends_the_given_request_id_so_the_backend_logs_this_call_under_it(self):
        seen_payload = {}

        def handler(request):
            if request.method == "POST":
                seen_payload.update(json.loads(request.content))
                return httpx.Response(202, json={"id": "search-1", "status": "queued"})
            return httpx.Response(200, json={"id": "search-1", "status": "completed", "response_status": 200, "result": SEARCH_RESPONSE_CONVERGED})

        client = _polling_client(handler)

        await client.search(query="women's trainers", answers_so_far=[], run_time_overrides={}, request_id="11111111-1111-1111-1111-111111111111")

        self.assertEqual(seen_payload["request_id"], "11111111-1111-1111-1111-111111111111")

    async def test_raises_validation_error_when_the_search_itself_fails_with_a_4xx(self):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(202, json={"id": "search-1", "status": "queued"})
            return httpx.Response(200, json={
                "id": "search-1", "status": "failed", "response_status": 422,
                "result": {"errors": [{"title": "Invalid query"}]},
            })

        client = _polling_client(handler)

        with self.assertRaises(TradeTariffBackendValidationError) as ctx:
            await client.search(query="women's trainers", answers_so_far=[], run_time_overrides={}, request_id="req-1")
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.body, {"errors": [{"title": "Invalid query"}]})

    async def test_raises_unavailable_error_when_the_search_itself_fails_with_a_5xx(self):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(202, json={"id": "search-1", "status": "queued"})
            return httpx.Response(200, json={"id": "search-1", "status": "failed", "response_status": 500, "result": {"errors": [{"title": "Search failed"}]}})

        client = _polling_client(handler)

        with self.assertRaises(TradeTariffBackendUnavailableError):
            await client.search(query="women's trainers", answers_so_far=[], run_time_overrides={}, request_id="req-1")

    async def test_raises_unavailable_error_if_the_search_never_finishes_in_time(self):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(202, json={"id": "search-1", "status": "queued"})
            return httpx.Response(200, json={"id": "search-1", "status": "running"})

        client = _polling_client(handler, poll_timeout_s=0.05)

        with self.assertRaises(TradeTariffBackendUnavailableError):
            await client.search(query="women's trainers", answers_so_far=[], run_time_overrides={}, request_id="req-1")


if __name__ == "__main__":
    unittest.main()
