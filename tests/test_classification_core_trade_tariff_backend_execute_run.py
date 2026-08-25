import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from classification_core.trade_tariff_backend.execute_run import execute_run
from classification_core_trade_tariff_backend_fixtures import (
    ATAR_RULING_RESPONSE,
    GOLD_QUERIES_RESPONSE,
    GOLD_QUERY_SIX_DIGIT,
    RUN_SHOW_RESPONSE,
)


class FakeClient:
    def __init__(self):
        self.update_run_calls = []
        self.update_run_kwargs = []
        self.post_result_calls = []
        self.run_attrs = RUN_SHOW_RESPONSE["data"]["attributes"] | {"id": "107"}
        self.gold_queries = [row["attributes"] | {"id": row["id"]} for row in GOLD_QUERIES_RESPONSE["data"]]
        self.atar = ATAR_RULING_RESPONSE["data"]["attributes"]

    async def get_run(self, run_id):
        assert run_id == "107"
        return self.run_attrs

    async def get_gold_queries(self):
        return self.gold_queries

    async def get_atar_ruling(self, ref):
        return self.atar

    async def update_run(self, run_id, status, **fields):
        self.update_run_calls.append(status)
        self.update_run_kwargs.append(fields)

    async def post_result(self, result):
        self.post_result_calls.append(result)


class ExecuteRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_marks_running_then_completed_and_posts_one_result_per_gold_query(self):
        client = FakeClient()

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=AsyncMock(return_value={
                "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                "converged": True, "simulator_failed": False,
            }),
        ):
            summary = await execute_run("107", client)

        self.assertEqual(client.update_run_calls, ["running", "completed"])
        self.assertEqual(len(client.post_result_calls), 1)
        self.assertEqual(client.post_result_calls[0]["final_code"], "6404199000")
        self.assertEqual(client.post_result_calls[0]["gold_in_top1"], True)
        self.assertEqual(client.post_result_calls[0]["final_rank"], 1)
        self.assertEqual(summary, {"status": "completed", "succeeded": 1, "failed": 0})

    async def test_the_qa_sessions_usage_totals_are_sent_on_the_posted_result(self):
        client = FakeClient()

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=AsyncMock(return_value={
                "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                "converged": True, "simulator_failed": False,
                "cost_usd": 0.0056, "latency_seconds": 1.15, "provider_calls": 2,
            }),
        ):
            await execute_run("107", client)

        posted = client.post_result_calls[0]
        self.assertAlmostEqual(posted["cost_usd"], 0.0056)
        self.assertAlmostEqual(posted["latency_seconds"], 1.15)
        self.assertEqual(posted["provider_calls"], 2)

    async def test_a_session_result_missing_usage_keys_posts_zero_rather_than_raising(self):
        # Defensive default, not the expected shape: every real
        # run_qa_session_via_trade_tariff_backend call always includes these
        # keys (see qa_loop.py), but plenty of other tests in this file mock
        # it with a bare {"final_candidates":..., "converged":...,
        # "simulator_failed":...} dict, and post_result must not KeyError on
        # those.
        client = FakeClient()

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=AsyncMock(return_value={
                "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                "converged": True, "simulator_failed": False,
            }),
        ):
            await execute_run("107", client)

        posted = client.post_result_calls[0]
        self.assertEqual(posted["cost_usd"], 0.0)
        self.assertEqual(posted["latency_seconds"], 0.0)
        self.assertEqual(posted["provider_calls"], 0)

    async def test_final_update_run_sends_completed_at_and_no_error_summary_when_nothing_failed(self):
        client = FakeClient()

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=AsyncMock(return_value={
                "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                "converged": True, "simulator_failed": False,
            }),
        ):
            await execute_run("107", client)

        final_kwargs = client.update_run_kwargs[-1]
        self.assertIsNotNone(final_kwargs["completed_at"])
        # Parseable ISO-8601 with a timezone — the Rails side stores it as a timestamp.
        self.assertIsNotNone(datetime.fromisoformat(final_kwargs["completed_at"]).tzinfo)
        self.assertIsNone(final_kwargs["error_summary"])

    async def test_one_gold_query_failing_does_not_abort_the_run(self):
        client = FakeClient()
        client.gold_queries = client.gold_queries * 2  # two gold queries this time

        async def flaky(*args, **kwargs):
            flaky.calls += 1
            if flaky.calls == 1:
                raise RuntimeError("simulator exhausted retries")
            return {"final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}], "converged": True, "simulator_failed": False}
        flaky.calls = 0

        with patch("classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend", new=flaky):
            summary = await execute_run("107", client)

        self.assertEqual(client.update_run_calls, ["running", "partially_failed"])
        self.assertEqual(len(client.post_result_calls), 2)
        self.assertIsNotNone(client.post_result_calls[0]["error"])
        self.assertIsNone(client.post_result_calls[1]["error"])
        self.assertEqual(summary, {"status": "partially_failed", "succeeded": 1, "failed": 1})
        # The run-level record has to say something failed, not just show a status.
        final_kwargs = client.update_run_kwargs[-1]
        self.assertEqual(final_kwargs["error_summary"], "1 of 2 gold queries failed")
        self.assertIsNotNone(final_kwargs["completed_at"])

    async def test_every_gold_query_failing_marks_the_run_failed(self):
        client = FakeClient()

        async def always_fails(*args, **kwargs):
            raise RuntimeError("search errored")

        with patch("classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend", new=always_fails):
            summary = await execute_run("107", client)

        self.assertEqual(client.update_run_calls, ["running", "failed"])
        self.assertEqual(summary, {"status": "failed", "succeeded": 0, "failed": 1})

    async def test_simulator_failure_is_recorded_as_a_failed_result_not_a_fabricated_success(self):
        # session_result["simulator_failed"] = True must never be scored as a real
        # answer — this is the exact bug the SDD review caught in Task 5: silently
        # falling back to a fabricated answer would corrupt gold_in_top1/gold_in_top5.
        client = FakeClient()

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=AsyncMock(return_value={
                "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                "converged": False, "simulator_failed": True,
            }),
        ):
            summary = await execute_run("107", client)

        self.assertEqual(client.update_run_calls, ["running", "failed"])
        self.assertEqual(len(client.post_result_calls), 1)
        self.assertIsNotNone(client.post_result_calls[0]["error"])
        self.assertFalse(client.post_result_calls[0]["gold_in_top1"])
        self.assertFalse(client.post_result_calls[0]["gold_in_top5"])
        self.assertEqual(summary, {"status": "failed", "succeeded": 0, "failed": 1})


class ScoringGranularityTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_six_digit_expected_code_matches_a_ten_digit_search_result(self):
        # expected_code is stored at whatever depth the ruling published — here
        # 6 digits. Search always returns 10. Comparing the two with plain `==`
        # scored a correct answer as wrong, with error=None, so it read as a
        # genuine accuracy measurement.
        client = FakeClient()
        client.gold_queries = [GOLD_QUERY_SIX_DIGIT]

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=AsyncMock(return_value={
                "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6403910000"}}],
                "converged": True, "simulator_failed": False,
            }),
        ):
            summary = await execute_run("107", client)

        posted = client.post_result_calls[0]
        self.assertTrue(posted["gold_in_top1"])
        self.assertTrue(posted["gold_in_top5"])
        self.assertEqual(posted["final_rank"], 1)
        # Only the comparison narrows to 6 digits — what we store as the answer
        # is still exactly what search returned.
        self.assertEqual(posted["final_code"], "6403910000")
        self.assertEqual(summary["status"], "completed")

    async def test_a_six_digit_expected_code_does_not_match_a_different_heading(self):
        client = FakeClient()
        client.gold_queries = [GOLD_QUERY_SIX_DIGIT]

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=AsyncMock(return_value={
                "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                "converged": True, "simulator_failed": False,
            }),
        ):
            await execute_run("107", client)

        posted = client.post_result_calls[0]
        self.assertFalse(posted["gold_in_top1"])
        self.assertFalse(posted["gold_in_top5"])
        self.assertIsNone(posted["final_rank"])

    async def test_a_missing_expected_code_digits_falls_back_to_the_codes_own_length(self):
        # Server-side expected_code_digits is just expected_code.length, so if the
        # field is ever absent the right degraded behaviour is to compare at the
        # code's actual length — not to score every candidate as a miss, which
        # would read as a real (but false) measurement.
        client = FakeClient()
        client.gold_queries = [GOLD_QUERY_SIX_DIGIT | {"expected_code_digits": None}]

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=AsyncMock(return_value={
                "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6403910000"}}],
                "converged": True, "simulator_failed": False,
            }),
        ):
            await execute_run("107", client)

        posted = client.post_result_calls[0]
        self.assertTrue(posted["gold_in_top1"])
        self.assertTrue(posted["gold_in_top5"])
        self.assertEqual(posted["final_rank"], 1)

    async def test_gold_in_top5_and_final_rank_find_a_match_below_the_first_candidate(self):
        client = FakeClient()
        client.gold_queries = [GOLD_QUERY_SIX_DIGIT]
        candidates = [
            {"attributes": {"goods_nomenclature_item_id": code}}
            for code in ["6404199000", "6402990000", "6403910000", "6405100000", "6406100000", "6401100000"]
        ]

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=AsyncMock(return_value={"final_candidates": candidates, "converged": True, "simulator_failed": False}),
        ):
            await execute_run("107", client)

        posted = client.post_result_calls[0]
        self.assertFalse(posted["gold_in_top1"])
        self.assertTrue(posted["gold_in_top5"])
        self.assertEqual(posted["final_rank"], 3)


class MalformedGoldQueryTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_gold_row_missing_its_fields_is_recorded_as_a_failure_not_an_escaped_exception(self):
        # The except handler must never read fields off the same dict the try
        # block just failed on — otherwise it raises too, escapes execute_run,
        # and strands the run at status="running" with nothing posted.
        client = FakeClient()
        client.gold_queries = [{"id": "99"}]  # no query/source_id/expected_code at all

        summary = await execute_run("107", client)

        self.assertEqual(client.update_run_calls, ["running", "failed"])
        self.assertEqual(len(client.post_result_calls), 1)
        self.assertIsNotNone(client.post_result_calls[0]["error"])
        self.assertEqual(summary, {"status": "failed", "succeeded": 0, "failed": 1})


class FailureRecordingItselfFailsClient(FakeClient):
    """A FakeClient whose post_result raises when handed a FAILURE-shaped
    payload (error is not None) but succeeds for a success-shaped one — models
    a malformed row getting rejected by a DB constraint, or the backend being
    briefly unreachable, specifically on the except handler's own write."""

    async def post_result(self, result):
        if result["error"] is not None:
            raise RuntimeError("backend rejected the failure record too")
        await super().post_result(result)


class GetGoldQueriesFailsClient(FakeClient):
    async def get_gold_queries(self):
        raise RuntimeError("gold queries endpoint unreachable")


class RunStrandingGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_except_handlers_own_post_result_call_raising_still_reaches_final_update_run(self):
        # This is the exact residual gap Fix Wave 2 closes: without an inner
        # try/except around the except handler's own post_result call, this
        # second exception would escape the whole function, skip every
        # remaining gold query, and skip the final update_run call — leaving
        # the run stuck at status="running" forever.
        client = FailureRecordingItselfFailsClient()
        client.gold_queries = client.gold_queries * 2  # two gold queries this time

        async def always_fails(*args, **kwargs):
            raise RuntimeError("search errored")

        with patch(
            "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
            new=always_fails,
        ):
            summary = await execute_run("107", client)

        # The final update_run call was still reached, with a real terminal
        # status — not left stuck at "running".
        self.assertEqual(client.update_run_calls, ["running", "failed"])
        # Neither gold query's failure-record write ever succeeded (both raised
        # inside FailureRecordingItselfFailsClient.post_result), but both are
        # still counted locally.
        self.assertEqual(len(client.post_result_calls), 0)
        self.assertEqual(summary, {"status": "failed", "succeeded": 0, "failed": 2})

    async def test_get_gold_queries_itself_raising_still_calls_final_update_run_with_status_failed(self):
        # The initial update_run(status="running") call happens BEFORE
        # get_gold_queries() is even called, so it is not covered by this
        # guard and must still fire. Only the block after it — including
        # get_gold_queries() — needs the outer try/except.
        client = GetGoldQueriesFailsClient()

        summary = await execute_run("107", client)

        self.assertEqual(client.update_run_calls, ["running", "failed"])
        self.assertEqual(len(client.post_result_calls), 0)
        self.assertEqual(summary, {"status": "failed", "succeeded": 0, "failed": 0})
        # The run is not left with a silent, uninformative failure — the
        # underlying exception is surfaced in error_summary even though no
        # per-gold-query failure count exists to build one from.
        final_kwargs = client.update_run_kwargs[-1]
        self.assertIn("gold queries endpoint unreachable", final_kwargs["error_summary"])


class NoOracleTextDisablesSimulatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_gold_query_with_no_oracle_text_is_run_with_sim_client_none(self):
        # An ATAR ruling with neither description nor justification leaves
        # oracle_text == "" — there is nothing grounding a simulated answer,
        # so the simulator must not be invoked for this gold query even though
        # a run-level sim_client exists.
        client = FakeClient()
        client.atar = {}  # no description, no justification -> oracle_text == ""

        with (
            patch(
                "classification_core.trade_tariff_backend.execute_run.openai_allowed",
                return_value=True,
            ),
            patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            patch(
                "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
                new=AsyncMock(return_value={
                    "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                    "converged": True, "simulator_failed": False,
                }),
            ) as mocked_qa_session,
        ):
            summary = await execute_run("107", client)

        mocked_qa_session.assert_awaited_once()
        self.assertIsNone(mocked_qa_session.await_args.kwargs["sim_client"])
        self.assertEqual(summary["status"], "completed")

    async def test_a_gold_query_with_oracle_text_still_gets_the_run_level_sim_client(self):
        # Companion to the test above: proves the fix varies per gold query
        # rather than accidentally disabling the simulator for the whole run
        # just because ONE gold query in it has no oracle text.
        client = FakeClient()  # default ATAR fixture has both description and justification

        with (
            patch(
                "classification_core.trade_tariff_backend.execute_run.openai_allowed",
                return_value=True,
            ),
            patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            patch(
                "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
                new=AsyncMock(return_value={
                    "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                    "converged": True, "simulator_failed": False,
                }),
            ) as mocked_qa_session,
        ):
            await execute_run("107", client)

        mocked_qa_session.assert_awaited_once()
        self.assertIsNotNone(mocked_qa_session.await_args.kwargs["sim_client"])


class ProgressLoggingTest(unittest.IsolatedAsyncioTestCase):
    async def test_prints_nothing_when_eval_progress_logging_is_unset(self):
        # Off by default everywhere unless explicitly set -- matches
        # CLASSIFICATION_ALLOW_PROVIDER_CALLS's convention.
        client = FakeClient()

        with (
            patch.dict(os.environ, {}, clear=False),
            patch(
                "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
                new=AsyncMock(return_value={
                    "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                    "converged": True, "simulator_failed": False, "questions_answered": 0,
                }),
            ),
        ):
            os.environ.pop("EVAL_PROGRESS_LOGGING", None)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                await execute_run("107", client)

        self.assertEqual(buffer.getvalue(), "")

    async def test_prints_the_full_progress_sequence_when_enabled(self):
        client = FakeClient()

        with (
            patch.dict(os.environ, {"EVAL_PROGRESS_LOGGING": "1"}),
            patch(
                "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
                new=AsyncMock(return_value={
                    "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                    "converged": True, "simulator_failed": False, "questions_answered": 2,
                }),
            ),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                await execute_run("107", client)

        output = buffer.getvalue()
        self.assertIn("1 gold quer", output)  # total count line
        self.assertIn("1/1", output)  # position
        self.assertIn("600004365", output)  # source_id
        self.assertIn("emu_generic", output)  # persona
        self.assertIn("women's trainers", output)  # query text
        self.assertIn("6404199000", output)  # final_code on the end line
        self.assertIn("questions_answered=2", output)
        self.assertIn("succeeded=1", output)
        self.assertIn("status=completed", output)

    async def test_accepts_the_other_true_values_not_just_1(self):
        for value in ("true", "YES", "On"):
            client = FakeClient()
            with (
                patch.dict(os.environ, {"EVAL_PROGRESS_LOGGING": value}),
                patch(
                    "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
                    new=AsyncMock(return_value={
                        "final_candidates": [{"attributes": {"goods_nomenclature_item_id": "6404199000"}}],
                        "converged": True, "simulator_failed": False, "questions_answered": 0,
                    }),
                ),
            ):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    await execute_run("107", client)

            self.assertNotEqual(buffer.getvalue(), "", f"expected output with EVAL_PROGRESS_LOGGING={value!r}")

    async def test_a_failed_gold_query_prints_the_error_on_its_end_line(self):
        client = FakeClient()

        with (
            patch.dict(os.environ, {"EVAL_PROGRESS_LOGGING": "1"}),
            patch(
                "classification_core.trade_tariff_backend.execute_run.run_qa_session_via_trade_tariff_backend",
                new=AsyncMock(side_effect=RuntimeError("search errored")),
            ),
        ):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                await execute_run("107", client)

        output = buffer.getvalue()
        self.assertIn("search errored", output)
        self.assertIn("status=failed", output)


if __name__ == "__main__":
    unittest.main()
