"""End-to-end wiring test: the REAL TradeTariffBackendClient driving the REAL
execute_run orchestrator, over an httpx.MockTransport serving this repo's own
fixtures.

Every other test in this subpackage uses a hand-authored fake client, so each
one only ever proved that the code agreed with the fake sitting next to it.
That is how get_gold_queries came to return raw JSON:API rows while execute_run
read them as flat dicts — no test ever put the two real pieces together. This
file is that missing seam: it exercises client -> HTTP shape -> orchestrator
with nothing hand-authored in between.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

import httpx

from classification_core.trade_tariff_backend.client import TradeTariffBackendClient
from classification_core.trade_tariff_backend.execute_run import execute_run
from classification_core_trade_tariff_backend_fixtures import (
    ATAR_RULING_RESPONSE,
    GOLD_QUERIES_RESPONSE,
    GOLD_QUERIES_RESPONSE_PAGE_1,
    GOLD_QUERIES_RESPONSE_PAGE_2,
    RESULT_POST_RESPONSE,
    RUN_SHOW_RESPONSE,
    RUN_UPDATE_RESPONSE,
    SEARCH_RESPONSE_CONVERGED,
)

# The spend gate is off for this test, deterministically: SEARCH_RESPONSE_CONVERGED
# never raises a clarifying question, so no simulator is needed, and this keeps
# the test from constructing an OpenAI client on a developer machine that
# happens to have the gate enabled.
NO_PROVIDER_CALLS = {"CLASSIFICATION_ALLOW_PROVIDER_CALLS": "", "OPENAI_API_KEY": ""}


class RecordingBackend:
    """Serves the real trade-tariff-backend routes from this repo's fixtures."""

    def __init__(self, gold_pages=None):
        self._gold_pages = gold_pages or {"1": GOLD_QUERIES_RESPONSE}
        self.requests = []
        self.posted_results = []
        self.run_updates = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))

        if path == "/uk/internal/evaluation_gold_queries":
            page = request.url.params.get("page", "1")
            return httpx.Response(200, json=self._gold_pages[page])
        if path.startswith("/uk/internal/atars/"):
            return httpx.Response(200, json=ATAR_RULING_RESPONSE)
        if path == "/uk/admin/search/evaluation/runs/107":
            if request.method == "PATCH":
                self.run_updates.append(json.loads(request.read())["data"]["attributes"])
                return httpx.Response(200, json=RUN_UPDATE_RESPONSE)
            return httpx.Response(200, json=RUN_SHOW_RESPONSE)
        if path == "/uk/admin/search/evaluation/searches":
            return httpx.Response(200, json=SEARCH_RESPONSE_CONVERGED)
        if path == "/uk/admin/search/evaluation/results":
            self.posted_results.extend(json.loads(request.read())["data"])
            return httpx.Response(201, json=RESULT_POST_RESPONSE)

        raise AssertionError(f"unexpected request to {request.method} {path}")

    def client(self):
        return TradeTariffBackendClient(base_url="http://backend.test", transport=httpx.MockTransport(self.handler))


class RealClientExecuteRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_run_completes_end_to_end_through_the_real_client(self):
        backend = RecordingBackend()
        client = backend.client()

        with patch.dict(os.environ, NO_PROVIDER_CALLS):
            summary = await execute_run("107", client)
        await client.aclose()

        self.assertEqual(summary, {"status": "completed", "succeeded": 1, "failed": 0})
        self.assertEqual(len(backend.posted_results), 1)
        self.assertEqual(len(backend.run_updates), 2)  # running, then completed
        self.assertEqual(backend.run_updates[0]["status"], "running")
        self.assertEqual(backend.run_updates[-1]["status"], "completed")
        self.assertIsNotNone(backend.run_updates[-1]["completed_at"])
        self.assertIsNone(backend.run_updates[-1]["error_summary"])

        # The gold row's flat attributes made it all the way through to the
        # posted result — the exact hop that was broken before this fix wave.
        posted = backend.posted_results[0]
        self.assertEqual(posted["source_type"], "atar")
        self.assertEqual(posted["source_id"], "600004365")
        self.assertEqual(posted["final_code"], "6404199000")
        self.assertTrue(posted["gold_in_top1"])
        self.assertEqual(posted["final_rank"], 1)

        # SEARCH_RESPONSE_CONVERGED's meta.usage (cost 0.0035, duration_ms 610,
        # 1 provider call) travels all the way through the real client and
        # qa_loop into the posted result -- the exact hop AI-1225 was missing.
        self.assertAlmostEqual(posted["cost_usd"], 0.0035)
        self.assertAlmostEqual(posted["latency_seconds"], 0.61)
        self.assertEqual(posted["provider_calls"], 1)

    async def test_every_page_of_a_multi_page_gold_set_is_executed(self):
        backend = RecordingBackend(gold_pages={"1": GOLD_QUERIES_RESPONSE_PAGE_1, "2": GOLD_QUERIES_RESPONSE_PAGE_2})
        client = backend.client()

        with patch.dict(os.environ, NO_PROVIDER_CALLS):
            summary = await execute_run("107", client)
        await client.aclose()

        # Two gold queries across two pages — a truncating client would report
        # "completed" here having silently scored only the first one.
        self.assertEqual(summary, {"status": "completed", "succeeded": 2, "failed": 0})
        self.assertEqual(len(backend.posted_results), 2)


if __name__ == "__main__":
    unittest.main()
