import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

import httpx

from classification_core.trade_tariff_backend.client import TradeTariffBackendClient
from classification_core_trade_tariff_backend_fixtures import SEARCH_RESPONSE_CONVERGED


class SearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_posts_query_answers_so_far_and_overrides_and_returns_the_raw_body(self):
        seen_payload = {}

        def handler(request):
            self.assertEqual(request.url.path, "/uk/admin/search/evaluation/searches")
            import json
            seen_payload.update(json.loads(request.content))
            return httpx.Response(200, json=SEARCH_RESPONSE_CONVERGED)

        client = TradeTariffBackendClient(base_url="http://backend.test", transport=httpx.MockTransport(handler))
        answers_so_far = [{"question": "What are the uppers made of?", "answer": "Textile", "options": ["Leather", "Textile"]}]

        result = await client.search(
            query="women's trainers", answers_so_far=answers_so_far, run_time_overrides={"max_rounds": 3},
        )

        self.assertEqual(seen_payload["q"], "women's trainers")
        self.assertEqual(seen_payload["answers"], answers_so_far)
        self.assertEqual(seen_payload["configuration_overrides"], {"max_rounds": 3})
        self.assertEqual(result, SEARCH_RESPONSE_CONVERGED)


if __name__ == "__main__":
    unittest.main()
