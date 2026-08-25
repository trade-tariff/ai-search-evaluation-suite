import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

import httpx

from classification_core.trade_tariff_backend.client import TradeTariffBackendClient, TradeTariffBackendUnavailableError, TradeTariffBackendValidationError
from classification_core_trade_tariff_backend_fixtures import (
    ATAR_RULING_RESPONSE,
    GOLD_QUERIES_RESPONSE,
    GOLD_QUERIES_RESPONSE_PAGE_1,
    GOLD_QUERIES_RESPONSE_PAGE_2,
    VALIDATION_ERROR_RESPONSE,
)


def _client_with_transport(handler):
    transport = httpx.MockTransport(handler)
    return TradeTariffBackendClient(base_url="http://backend.test", transport=transport)


def _flattened(response):
    return [row["attributes"] | {"id": row["id"]} for row in response["data"]]


class GetGoldQueriesTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_flattened_rows_on_success(self):
        # Flattened, not raw JSON:API rows: execute_run reads gold["source_type"]
        # as a flat key, and every other method on this client flattens too.
        def handler(request):
            self.assertEqual(request.url.path, "/uk/internal/evaluation_gold_queries")
            return httpx.Response(200, json=GOLD_QUERIES_RESPONSE)

        client = _client_with_transport(handler)
        result = await client.get_gold_queries()

        self.assertEqual(result, _flattened(GOLD_QUERIES_RESPONSE))
        self.assertEqual(result[0]["source_type"], "atar")
        self.assertEqual(result[0]["id"], "1")

    async def test_follows_pagination_until_every_row_has_been_collected(self):
        # The Rails controller caps a page at per_page rows (DEFAULT_PER_PAGE=100),
        # so a single request would silently truncate a larger gold set and the
        # run would score only part of it while still reporting "completed".
        pages = []

        def handler(request):
            pages.append(request.url.params["page"])
            if request.url.params["page"] == "1":
                return httpx.Response(200, json=GOLD_QUERIES_RESPONSE_PAGE_1)
            return httpx.Response(200, json=GOLD_QUERIES_RESPONSE_PAGE_2)

        client = _client_with_transport(handler)
        result = await client.get_gold_queries()

        self.assertEqual(pages, ["1", "2"])
        self.assertEqual(result, _flattened(GOLD_QUERIES_RESPONSE_PAGE_1) + _flattened(GOLD_QUERIES_RESPONSE_PAGE_2))
        self.assertEqual([row["id"] for row in result], ["1", "2"])

    async def test_requests_the_largest_page_size_the_backend_allows(self):
        def handler(request):
            self.assertEqual(request.url.params["per_page"], "250")  # controller's MAX_PER_PAGE
            return httpx.Response(200, json=GOLD_QUERIES_RESPONSE)

        client = _client_with_transport(handler)
        await client.get_gold_queries()

    async def test_raises_validation_error_on_4xx_without_retrying(self):
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(422, json=VALIDATION_ERROR_RESPONSE)

        client = _client_with_transport(handler)

        with self.assertRaises(TradeTariffBackendValidationError):
            await client.get_gold_queries()
        self.assertEqual(attempts, 1)

    async def test_raises_backend_unavailable_after_exhausting_retries_on_5xx(self):
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, json={"errors": [{"detail": "unavailable"}]})

        client = _client_with_transport(handler)

        with self.assertRaises(TradeTariffBackendUnavailableError):
            await client.get_gold_queries()
        self.assertGreater(attempts, 1)

    async def test_succeeds_after_one_transient_5xx_then_a_200(self):
        attempts = 0

        def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, json={"errors": [{"detail": "unavailable"}]})
            return httpx.Response(200, json=GOLD_QUERIES_RESPONSE)

        client = _client_with_transport(handler)
        result = await client.get_gold_queries()

        self.assertEqual(result, _flattened(GOLD_QUERIES_RESPONSE))
        self.assertEqual(attempts, 2)


class GetAtarRulingTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_ruling_attributes_by_ref(self):
        def handler(request):
            self.assertEqual(request.url.path, "/uk/internal/atars/600004365")
            return httpx.Response(200, json=ATAR_RULING_RESPONSE)

        client = _client_with_transport(handler)
        result = await client.get_atar_ruling("600004365")

        self.assertEqual(result, ATAR_RULING_RESPONSE["data"]["attributes"])


if __name__ == "__main__":
    unittest.main()
