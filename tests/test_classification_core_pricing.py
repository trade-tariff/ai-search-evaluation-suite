import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from classification_core.pricing import calculate_cost


class CalculateCostTest(unittest.TestCase):
    def test_computes_cost_from_input_and_output_tokens_for_a_known_model(self):
        usage = SimpleNamespace(prompt_tokens=1_000_000, completion_tokens=1_000_000)

        cost_usd, pricing_known = calculate_cost("gpt-5.4", usage)

        # gpt-5.4: $2.50/1M input + $15.00/1M output, per config/openai_model_pricing.yml
        self.assertAlmostEqual(cost_usd, 2.5 + 15.0)
        self.assertTrue(pricing_known)

    def test_accepts_a_plain_dict_as_well_as_an_sdk_usage_object(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}

        cost_usd, pricing_known = calculate_cost("gpt-5.4", usage)

        self.assertAlmostEqual(cost_usd, 2.5)
        self.assertTrue(pricing_known)

    def test_an_unpriced_model_returns_none_cost_and_pricing_known_false_rather_than_raising(self):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50)

        cost_usd, pricing_known = calculate_cost("some-future-model-not-in-the-table", usage)

        self.assertIsNone(cost_usd)
        self.assertFalse(pricing_known)

    def test_missing_usage_returns_none_cost_and_pricing_known_false_rather_than_raising(self):
        cost_usd, pricing_known = calculate_cost("gpt-5.4", None)

        self.assertIsNone(cost_usd)
        self.assertFalse(pricing_known)

    def test_missing_token_fields_are_treated_as_zero_not_an_error(self):
        usage = SimpleNamespace(prompt_tokens=None, completion_tokens=None)

        cost_usd, pricing_known = calculate_cost("gpt-5.4", usage)

        self.assertAlmostEqual(cost_usd, 0.0)
        self.assertTrue(pricing_known)


if __name__ == "__main__":
    unittest.main()
