import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

import httpx

from classification_core.trade_tariff_backend.client import TradeTariffBackendClient, TradeTariffBackendValidationError
from classification_core_trade_tariff_backend_fixtures import RESULT_POST_RESPONSE, RESULT_POST_VALIDATION_ERROR_RESPONSE


class PostResultTest(unittest.IsolatedAsyncioTestCase):
    async def test_wraps_the_single_result_in_a_one_item_bulk_array_with_no_attributes_wrapper(self):
        seen_payload = {}

        def handler(request):
            self.assertEqual(request.url.path, "/uk/admin/search/evaluation/results")
            import json
            seen_payload.update(json.loads(request.content))
            return httpx.Response(200, json=RESULT_POST_RESPONSE)

        client = TradeTariffBackendClient(base_url="http://backend.test", transport=httpx.MockTransport(handler))
        result = {
            "run_id": "107", "source_type": "atar", "source_id": "600004365", "persona": "emu_generic",
            "expected_code": "6404199000", "final_code": "6404199000",
            "gold_in_top1": True, "gold_in_top5": True,
        }

        response = await client.post_result(result)

        self.assertEqual(seen_payload["data"], [result])
        self.assertEqual(response["id"], "9")

    async def test_raises_backend_validation_error_when_the_single_item_is_rejected(self):
        def handler(request):
            return httpx.Response(422, json=RESULT_POST_VALIDATION_ERROR_RESPONSE)

        client = TradeTariffBackendClient(base_url="http://backend.test", transport=httpx.MockTransport(handler))

        with self.assertRaises(TradeTariffBackendValidationError):
            await client.post_result({"run_id": "does-not-exist", "source_type": "atar", "source_id": "x", "persona": "emu_generic"})


if __name__ == "__main__":
    unittest.main()
