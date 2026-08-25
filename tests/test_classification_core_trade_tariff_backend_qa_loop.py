import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from classification_core.trade_tariff_backend.qa_loop import run_qa_session_via_trade_tariff_backend
from classification_core_trade_tariff_backend_fixtures import (
    SEARCH_RESPONSE_CONVERGED,
    SEARCH_RESPONSE_CONVERGED_NO_USAGE,
    SEARCH_RESPONSE_PENDING_QUESTION,
)


class FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def search(self, query, answers_so_far, run_time_overrides):
        self.calls.append({"query": query, "answers_so_far": list(answers_so_far), "run_time_overrides": run_time_overrides})
        return self._responses.pop(0)


class RunQaSessionViaBackendTest(unittest.IsolatedAsyncioTestCase):
    async def test_stops_as_soon_as_a_round_has_no_pending_question(self):
        client = FakeClient([SEARCH_RESPONSE_CONVERGED])

        result = await run_qa_session_via_trade_tariff_backend(
            client=client, sim_client=None, query="women's trainers",
            oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
        )

        self.assertTrue(result["converged"])
        self.assertEqual(result["final_candidates"], SEARCH_RESPONSE_CONVERGED["data"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["answers_so_far"], [])

    async def test_a_single_round_with_usage_reports_that_rounds_totals(self):
        # SEARCH_RESPONSE_CONVERGED carries meta.usage: total_cost_usd 0.0035,
        # duration_ms 610, provider_calls 1 -- see the fixtures file.
        client = FakeClient([SEARCH_RESPONSE_CONVERGED])

        result = await run_qa_session_via_trade_tariff_backend(
            client=client, sim_client=None, query="women's trainers",
            oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
        )

        self.assertAlmostEqual(result["cost_usd"], 0.0035)
        self.assertAlmostEqual(result["latency_seconds"], 0.61)
        self.assertEqual(result["provider_calls"], 1)

    async def test_usage_accumulates_across_multiple_rounds_not_just_the_last_one(self):
        # Round 1 (SEARCH_RESPONSE_PENDING_QUESTION): cost 0.0021, duration_ms 540, 1 call.
        # Round 2 (SEARCH_RESPONSE_CONVERGED): cost 0.0035, duration_ms 610, 1 call.
        client = FakeClient([SEARCH_RESPONSE_PENDING_QUESTION, SEARCH_RESPONSE_CONVERGED])

        with patch(
            "classification_core.trade_tariff_backend.qa_loop.simulate_trader_answer",
            new=AsyncMock(return_value={
                "chosen": "Textile", "choice_index": 1, "slot": "material", "reasoning": "",
                "simulator_failed": False, "attempts": 1, "last_error": None,
            }),
        ):
            result = await run_qa_session_via_trade_tariff_backend(
                client=client, sim_client="fake-openai-client", query="women's trainers",
                oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
            )

        self.assertAlmostEqual(result["cost_usd"], 0.0021 + 0.0035)
        self.assertAlmostEqual(result["latency_seconds"], (540 + 610) / 1000)
        # 1 retrieval call (round 1) + 1 simulator call (answering round 1's
        # question, per the mocked "attempts": 1) + 1 retrieval call (round 2).
        self.assertEqual(result["provider_calls"], 3)

    async def test_the_simulators_own_cost_and_duration_fold_into_the_same_totals_as_retrieval(self):
        # Retrieval side (round 1): cost 0.0021, 540ms, 1 call (SEARCH_RESPONSE_PENDING_QUESTION).
        # Simulator side (answering round 1's question): cost 0.004, 2.5s, 2 calls (a retry).
        # Retrieval side (round 2): cost 0.0035, 610ms, 1 call (SEARCH_RESPONSE_CONVERGED).
        client = FakeClient([SEARCH_RESPONSE_PENDING_QUESTION, SEARCH_RESPONSE_CONVERGED])

        with patch(
            "classification_core.trade_tariff_backend.qa_loop.simulate_trader_answer",
            new=AsyncMock(return_value={
                "chosen": "Textile", "choice_index": 1, "slot": "material", "reasoning": "",
                "simulator_failed": False, "attempts": 2, "last_error": None,
                "cost_usd": 0.004, "duration_seconds": 2.5, "pricing_known": True,
            }),
        ):
            result = await run_qa_session_via_trade_tariff_backend(
                client=client, sim_client="fake-openai-client", query="women's trainers",
                oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
            )

        self.assertAlmostEqual(result["cost_usd"], 0.0021 + 0.004 + 0.0035)
        self.assertAlmostEqual(result["latency_seconds"], 0.54 + 2.5 + 0.61)
        # 1 retrieval call (round 1) + 2 simulator calls (a retry) + 1 retrieval call (round 2).
        self.assertEqual(result["provider_calls"], 4)

    async def test_a_failed_simulator_attempt_still_counts_its_real_cost_before_the_early_return(self):
        # The simulator exhausted its retries (simulator_failed=True) -- the loop stops
        # immediately per the existing behaviour, but the failed attempts still burned
        # real, costly API calls that must not be reported as free.
        client = FakeClient([SEARCH_RESPONSE_PENDING_QUESTION])

        with patch(
            "classification_core.trade_tariff_backend.qa_loop.simulate_trader_answer",
            new=AsyncMock(return_value={
                "chosen": None, "choice_index": None, "slot": "failed_round_1", "reasoning": "",
                "simulator_failed": True, "attempts": 3, "last_error": "could not parse response",
                "cost_usd": 0.006, "duration_seconds": 4.1, "pricing_known": True,
            }),
        ):
            result = await run_qa_session_via_trade_tariff_backend(
                client=client, sim_client="fake-openai-client", query="women's trainers",
                oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
            )

        self.assertTrue(result["simulator_failed"])
        self.assertAlmostEqual(result["cost_usd"], 0.0021 + 0.006)
        self.assertAlmostEqual(result["latency_seconds"], 0.54 + 4.1)
        self.assertEqual(result["provider_calls"], 1 + 3)

    async def test_a_round_with_no_usage_key_contributes_nothing_rather_than_raising(self):
        # meta.usage is absent entirely on a short-circuit round (no LLM call
        # made) -- must not raise or be treated as an error.
        client = FakeClient([SEARCH_RESPONSE_CONVERGED_NO_USAGE])

        result = await run_qa_session_via_trade_tariff_backend(
            client=client, sim_client=None, query="women's trainers",
            oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
        )

        self.assertEqual(result["cost_usd"], 0.0)
        self.assertEqual(result["latency_seconds"], 0.0)
        self.assertEqual(result["provider_calls"], 0)

    async def test_answers_a_pending_question_via_simulate_trader_answer_then_calls_search_again(self):
        client = FakeClient([SEARCH_RESPONSE_PENDING_QUESTION, SEARCH_RESPONSE_CONVERGED])

        with patch(
            "classification_core.trade_tariff_backend.qa_loop.simulate_trader_answer",
            new=AsyncMock(return_value={
                "chosen": "Textile", "choice_index": 1, "slot": "material", "reasoning": "",
                "simulator_failed": False, "attempts": 1, "last_error": None,
            }),
        ) as mocked_answer:
            result = await run_qa_session_via_trade_tariff_backend(
                client=client, sim_client="fake-openai-client", query="women's trainers",
                oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
            )

        self.assertTrue(result["converged"])
        self.assertEqual(len(client.calls), 2)
        # Second call must carry the answer forward as the accumulating Q&A history.
        self.assertEqual(
            client.calls[1]["answers_so_far"],
            [{"question": "What are the uppers made of?", "answer": "Textile", "options": ["Leather", "Textile", "Man-made", "Other"]}],
        )
        mocked_answer.assert_awaited_once()

    async def test_fails_immediately_without_calling_the_simulator_when_no_sim_client_is_configured(self):
        # With the spend gate off there is no simulator client to answer with.
        # That must fail fast and for the right reason, not call
        # simulate_trader_answer(client=None) and burn its retries on an
        # AttributeError before reporting the same failure.
        client = FakeClient([SEARCH_RESPONSE_PENDING_QUESTION])

        with patch(
            "classification_core.trade_tariff_backend.qa_loop.simulate_trader_answer",
            new=AsyncMock(),
        ) as mocked_answer:
            result = await run_qa_session_via_trade_tariff_backend(
                client=client, sim_client=None, query="women's trainers",
                oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
            )

        mocked_answer.assert_not_called()
        self.assertTrue(result["simulator_failed"])
        self.assertFalse(result["converged"])
        self.assertEqual(result["final_candidates"], SEARCH_RESPONSE_PENDING_QUESTION["data"])
        self.assertEqual(len(client.calls), 1)

    async def test_stops_at_max_rounds_without_converging_if_questions_never_stop(self):
        # Every response still has a pending question — max_rounds must cap the loop.
        client = FakeClient([SEARCH_RESPONSE_PENDING_QUESTION] * 4)

        with patch(
            "classification_core.trade_tariff_backend.qa_loop.simulate_trader_answer",
            new=AsyncMock(return_value={
                "chosen": "Textile", "choice_index": 1, "slot": "material", "reasoning": "",
                "simulator_failed": False, "attempts": 1, "last_error": None,
            }),
        ):
            result = await run_qa_session_via_trade_tariff_backend(
                client=client, sim_client="fake-openai-client", query="women's trainers",
                oracle_text="ruling text", run_time_overrides={}, max_rounds=2,
            )

        self.assertFalse(result["converged"])
        self.assertEqual(len(client.calls), 2)

    async def test_stops_and_reports_failure_when_the_simulator_exhausts_retries(self):
        # A fabricated answer must never be silently fed back to search() — that
        # would corrupt the eval metrics this whole suite exists to measure.
        client = FakeClient([SEARCH_RESPONSE_PENDING_QUESTION])

        with patch(
            "classification_core.trade_tariff_backend.qa_loop.simulate_trader_answer",
            new=AsyncMock(return_value={
                "chosen": None, "choice_index": None, "slot": "failed_round_1", "reasoning": "",
                "simulator_failed": True, "attempts": 3, "last_error": "could not parse response",
            }),
        ):
            result = await run_qa_session_via_trade_tariff_backend(
                client=client, sim_client="fake-openai-client", query="women's trainers",
                oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
            )

        self.assertTrue(result["simulator_failed"])
        self.assertFalse(result["converged"])
        # Must stop immediately on failure, not keep looping with a fabricated answer.
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
