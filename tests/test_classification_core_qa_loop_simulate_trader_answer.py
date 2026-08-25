import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from classification_core.pricing import calculate_cost
from classification_core.qa_loop import SessionFacts, simulate_trader_answer


def _response(content, prompt_tokens=100, completion_tokens=20):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


VALID_CONTENT = '{"slot": "material", "choice_index": 1, "reasoning": "textile fits best"}'
UNPARSEABLE_CONTENT = "not json at all"

# SIMULATOR_MODEL's real default (QA_SIMULATOR_MODEL env var, or "gpt-5-mini" if
# unset) does not exactly match any key in pricing.MODEL_PRICING -- Rails' own
# pricing table keys the equivalent model as "gpt-5-mini-2025-08-07" (dated), not
# "gpt-5-mini". These cost-accounting tests patch SIMULATOR_MODEL to a model that
# IS in the table, so they test the summing logic itself rather than that one
# specific naming mismatch -- see test_the_real_default_model_is_not_in_the_pricing_table
# below, which documents that mismatch explicitly rather than leaving it implicit.
KNOWN_PRICED_MODEL = "gpt-5.4"


class FakeOpenAIClient:
    def __init__(self, responses=None, side_effect=None):
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(side_effect=side_effect or responses))
        )


@patch("classification_core.qa_loop.SIMULATOR_MODEL", KNOWN_PRICED_MODEL)
class SimulateTraderAnswerCostTrackingTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_successful_first_attempt_reports_that_calls_cost_and_duration(self):
        client = FakeOpenAIClient(responses=[_response(VALID_CONTENT, prompt_tokens=1000, completion_tokens=200)])

        result = await simulate_trader_answer(
            client=client, session=SessionFacts(), raw_query="women's trainers",
            question="What are the uppers made of?", options=["Leather", "Textile"], round_number=1,
        )

        self.assertFalse(result["simulator_failed"])
        self.assertTrue(result["pricing_known"])
        self.assertGreater(result["cost_usd"], 0)
        self.assertGreaterEqual(result["duration_seconds"], 0)
        self.assertEqual(result["attempts"], 1)

    async def test_a_retry_after_an_unparseable_response_sums_cost_across_both_attempts(self):
        # First call returns unparseable content (burns real tokens/cost even
        # though it fails to parse); second call succeeds. Both must count.
        first = _response(UNPARSEABLE_CONTENT, prompt_tokens=100, completion_tokens=20)
        second = _response(VALID_CONTENT, prompt_tokens=150, completion_tokens=30)
        client = FakeOpenAIClient(responses=[first, second])

        result = await simulate_trader_answer(
            client=client, session=SessionFacts(), raw_query="women's trainers",
            question="What are the uppers made of?", options=["Leather", "Textile"], round_number=1,
        )

        self.assertFalse(result["simulator_failed"])
        self.assertEqual(result["attempts"], 2)
        expected_cost_1, _ = calculate_cost(KNOWN_PRICED_MODEL, SimpleNamespace(prompt_tokens=100, completion_tokens=20))
        expected_cost_2, _ = calculate_cost(KNOWN_PRICED_MODEL, SimpleNamespace(prompt_tokens=150, completion_tokens=30))
        self.assertAlmostEqual(result["cost_usd"], expected_cost_1 + expected_cost_2)

    async def test_exhausting_every_retry_still_reports_the_real_cost_incurred(self):
        # A failed simulator attempt (never produces a usable answer) still made
        # real, costly API calls -- must not be reported as free.
        client = FakeOpenAIClient(responses=[_response(UNPARSEABLE_CONTENT)] * 3)  # SIMULATOR_MAX_RETRIES + 1

        result = await simulate_trader_answer(
            client=client, session=SessionFacts(), raw_query="women's trainers",
            question="What are the uppers made of?", options=["Leather", "Textile"], round_number=1,
        )

        self.assertTrue(result["simulator_failed"])
        self.assertEqual(result["attempts"], 3)
        self.assertGreater(result["cost_usd"], 0)

    async def test_a_network_level_failure_contributes_zero_cost_since_no_usage_data_exists(self):
        # The except branch (API call itself raised, e.g. a network error) never
        # gets a usage object back -- nothing to attribute cost to for that attempt.
        client = FakeOpenAIClient(side_effect=[
            RuntimeError("connection reset"),
            _response(VALID_CONTENT),
        ])

        result = await simulate_trader_answer(
            client=client, session=SessionFacts(), raw_query="women's trainers",
            question="What are the uppers made of?", options=["Leather", "Textile"], round_number=1,
        )

        self.assertFalse(result["simulator_failed"])
        self.assertEqual(result["attempts"], 2)
        # Only the second (successful) attempt has usage data to cost.
        expected_cost, _ = calculate_cost(KNOWN_PRICED_MODEL, SimpleNamespace(prompt_tokens=100, completion_tokens=20))
        self.assertAlmostEqual(result["cost_usd"], expected_cost)

    async def test_no_options_short_circuit_reports_zero_cost_with_no_api_call_made(self):
        client = FakeOpenAIClient(responses=[])

        result = await simulate_trader_answer(
            client=client, session=SessionFacts(), raw_query="women's trainers",
            question="What are the uppers made of?", options=[], round_number=1,
        )

        self.assertTrue(result["simulator_failed"])
        self.assertEqual(result["cost_usd"], 0.0)
        self.assertEqual(result["duration_seconds"], 0.0)
        client.chat.completions.create.assert_not_called()

    async def test_an_unpriced_model_reports_pricing_known_false_and_zero_cost_not_a_crash(self):
        with patch("classification_core.qa_loop.SIMULATOR_MODEL", "some-future-model-not-in-the-table"):
            client = FakeOpenAIClient(responses=[_response(VALID_CONTENT)])

            result = await simulate_trader_answer(
                client=client, session=SessionFacts(), raw_query="women's trainers",
                question="What are the uppers made of?", options=["Leather", "Textile"], round_number=1,
            )

        self.assertFalse(result["simulator_failed"])
        self.assertFalse(result["pricing_known"])
        self.assertEqual(result["cost_usd"], 0.0)


class SimulateTraderAnswerDefaultModelPricingTest(unittest.TestCase):
    def test_the_real_default_model_is_not_in_the_pricing_table(self):
        # Documents a real, known gap rather than leaving it implicit: out of the
        # box (QA_SIMULATOR_MODEL unset), the simulator's cost will report as
        # pricing_known=False until an operator sets QA_SIMULATOR_MODEL to one of
        # pricing.MODEL_PRICING's exact (dated) keys, e.g. "gpt-5-mini-2025-08-07".
        from classification_core.qa_loop import SIMULATOR_MODEL
        from classification_core.pricing import MODEL_PRICING

        self.assertNotIn(SIMULATOR_MODEL, MODEL_PRICING)


if __name__ == "__main__":
    unittest.main()
