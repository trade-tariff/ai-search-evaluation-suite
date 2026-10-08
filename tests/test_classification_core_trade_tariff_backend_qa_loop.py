import copy
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

    async def search(self, query, answers_so_far, run_time_overrides, request_id=None):
        self.calls.append({"query": query, "answers_so_far": list(answers_so_far), "run_time_overrides": run_time_overrides, "request_id": request_id})
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
        self.assertEqual(result["provider_calls"], 2)

    async def test_questions_answered_counts_the_simulators_committed_answers(self):
        # AI-1223 console progress logging needs this to print "N questions
        # answered" per gold query -- answers_so_far already tracks this
        # locally but was never surfaced on the return value.
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

        self.assertEqual(result["questions_answered"], 1)

    async def test_questions_answered_is_zero_when_the_first_round_already_converges(self):
        client = FakeClient([SEARCH_RESPONSE_CONVERGED])

        result = await run_qa_session_via_trade_tariff_backend(
            client=client, sim_client=None, query="women's trainers",
            oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
        )

        self.assertEqual(result["questions_answered"], 0)

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
        # No usage at all is not the same as "a call we couldn't price" -- there was no call,
        # so nothing here casts doubt on the (zero) cost.
        self.assertTrue(result["pricing_known"])

    async def test_pricing_known_is_true_when_every_rounds_usage_was_priced(self):
        client = FakeClient([SEARCH_RESPONSE_CONVERGED])

        result = await run_qa_session_via_trade_tariff_backend(
            client=client, sim_client=None, query="women's trainers",
            oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
        )

        self.assertTrue(result["pricing_known"])

    async def test_pricing_known_is_false_when_any_rounds_usage_used_an_unpriced_model(self):
        # AiUsage::PricingCalculator#pricing_known? is false whenever a call used a model
        # missing from config/openai_model_pricing.yml in trade-tariff-backend -- cost_usd for
        # that round is still whatever partial total could be priced, not nil, so this flag is
        # the only signal that the accumulated total might be understated.
        unpriced_response = copy.deepcopy(SEARCH_RESPONSE_CONVERGED)
        unpriced_response["meta"]["usage"]["pricing_known"] = False
        client = FakeClient([unpriced_response])

        result = await run_qa_session_via_trade_tariff_backend(
            client=client, sim_client=None, query="women's trainers",
            oracle_text="ruling text", run_time_overrides={}, max_rounds=4,
        )

        self.assertFalse(result["pricing_known"])

    async def test_pricing_known_is_false_overall_if_any_round_among_several_was_unpriced(self):
        unpriced_pending = copy.deepcopy(SEARCH_RESPONSE_PENDING_QUESTION)
        unpriced_pending["meta"]["usage"]["pricing_known"] = False
        client = FakeClient([unpriced_pending, SEARCH_RESPONSE_CONVERGED])

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

        self.assertFalse(result["pricing_known"])

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

    async def test_question_trace_records_every_round_including_a_failed_one(self):
        search_responses = [
            {"data": [], "meta": {"interactive_search": {"answers": [{"question": "What material?", "options": ["Rubber", "Leather"], "answer": None}]}}},
            {"data": [], "meta": {"interactive_search": {"answers": [{"question": "What closure?", "options": ["Laces", "Velcro"], "answer": None}]}}},
        ]
        client = AsyncMock()
        client.search = AsyncMock(side_effect=search_responses)

        sim_results = [
            {"chosen": "Rubber", "choice_index": 0, "slot": "material", "reasoning": "oracle text says rubber sole", "simulator_failed": False, "attempts": 1, "last_error": None},
            {"chosen": None, "choice_index": None, "slot": None, "reasoning": None, "simulator_failed": True, "attempts": 3, "last_error": "could not parse a choice"},
        ]
        with patch("classification_core.trade_tariff_backend.qa_loop.simulate_trader_answer", new=AsyncMock(side_effect=sim_results)):
            result = await run_qa_session_via_trade_tariff_backend(
                client=client, sim_client=object(), query="boots", oracle_text="Rubber-soled leather boots.",
                run_time_overrides={}, max_rounds=5,
            )

        self.assertEqual(len(result["question_trace"]), 2)
        # request_id is a fresh UUID generated by the loop itself, so its exact value isn't asserted —
        # only that one was actually generated and sent (not blank/reused), and that the value sent to
        # client.search for round 1 is the exact same value recorded on round 1's own trace entry, not
        # round 2's (the whole point of capturing it is to point at the specific round it belongs to).
        first_call_request_id = client.search.call_args_list[0].kwargs["request_id"]
        second_call_request_id = client.search.call_args_list[1].kwargs["request_id"]
        self.assertTrue(first_call_request_id)
        self.assertNotEqual(first_call_request_id, second_call_request_id)
        self.assertEqual(result["question_trace"][0], {
            "round": 1, "question": "What material?", "options": ["Rubber", "Leather"],
            "chosen": "Rubber", "choice_index": 0, "reasoning": "oracle text says rubber sole",
            "attempts": 1, "simulator_failed": False, "request_id": first_call_request_id,
        })
        self.assertEqual(result["question_trace"][1]["simulator_failed"], True)
        self.assertEqual(result["question_trace"][1]["round"], 2)
        self.assertEqual(result["question_trace"][1]["request_id"], second_call_request_id)
        self.assertTrue(result["simulator_failed"])

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
