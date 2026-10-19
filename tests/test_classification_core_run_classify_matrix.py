"""Regression tests for the classification matrix harness (run_classify_matrix).

Pins the session-fact authority policy (ATAR-backed oracle answers -> tier 5,
anything else -> tier 8) and the failure reporting: run_config counts failed
sessions and main exits 1 when no session produced a result.

No database, network, or provider calls: run_qa_session and
persist_session_facts_to_kg are patched, and the psycopg connection is faked.
"""
import asyncio
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

import classification_core.run_classify_matrix as classify_matrix


ATAR_GOLD = {
    "id": 12,
    "source_id": "600004365",
    "source_type": "atar",
    "persona": "naive_vague",
    "query": "women's trainers",
    "expected_code": "6404199000",
}


def _qa_session_result():
    return {
        "rounds": [{"round_number": 1, "chosen": "textile", "answer_source": "simulator"}],
        "final_mode": "answers",
        "final_answers": [{"commodity_code": "6404199000"}],
        "total_classify_calls": 1,
        "total_simulator_calls": 1,
        "facts": [{"key": "material", "value": "textile", "confidence": 0.9}],
    }


class SessionFactAuthorityTierTest(unittest.TestCase):
    def test_an_atar_backed_oracle_answer_rates_as_an_extracted_atar_fact(self):
        self.assertEqual(classify_matrix._session_fact_authority_tier(
            ATAR_GOLD, "ruling text", _qa_session_result()["rounds"],
        ), 5)

    def test_an_atar_source_id_alone_still_rates_as_atar_backed(self):
        gold = {**ATAR_GOLD, "source_type": "", "source_id": "ATAR_600004365"}
        self.assertEqual(classify_matrix._session_fact_authority_tier(
            gold, "ruling text", _qa_session_result()["rounds"],
        ), 5)

    def test_a_non_atar_gold_stays_at_the_low_trader_assertion_tier(self):
        gold = {**ATAR_GOLD, "source_type": "trader_session", "source_id": "session:99"}
        self.assertEqual(classify_matrix._session_fact_authority_tier(
            gold, "ruling text", _qa_session_result()["rounds"],
        ), 8)

    def test_missing_oracle_never_receives_atar_authority(self):
        for oracle in ("", "   "):
            with self.subTest(oracle=oracle):
                self.assertEqual(classify_matrix._session_fact_authority_tier(
                    ATAR_GOLD, oracle, _qa_session_result()["rounds"],
                ), 8)

    def test_fallback_human_mixed_and_unknown_provenance_stay_low_authority(self):
        for source in ("fallback_first_option", "human", None):
            with self.subTest(source=source):
                rounds = _qa_session_result()["rounds"] + [{"chosen": "textile", "answer_source": source}]
                self.assertEqual(classify_matrix._session_fact_authority_tier(
                    ATAR_GOLD, "ruling text", rounds,
                ), 8)
        self.assertEqual(classify_matrix._session_fact_authority_tier(ATAR_GOLD), 8)


class RunOneSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_completed_qa_session_returns_scored_results(self):
        # The full session must reach scoring without an undefined helper.
        with (
            patch(
                "classification_core.qa_loop.run_qa_session",
                new=AsyncMock(return_value=_qa_session_result()),
            ),
            patch(
                "classification_core.qa_loop.persist_session_facts_to_kg",
            ),
        ):
            result = await classify_matrix.run_one_session(
                ATAR_GOLD, "ruling text", strategy="converge", prompt_mode="baseline",
                augmentation="facts+kg", model="gpt-5-mini",
                candidate_limit=40, max_rounds=5,
            )

        self.assertEqual(result["gold_in_final_set"], True)
        self.assertEqual(result["gold_in_top1"], True)
        self.assertIn("persisted_session_facts", result["trace_json"])


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """Serves one pre-set row list per `with conn.cursor()` block, in order."""

    def __init__(self, row_sets):
        self._row_sets = list(row_sets)
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self._row_sets.pop(0) if self._row_sets else [])

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def _gold_rows(n=3):
    return [
        {
            "id": i,
            "source_id": f"60000436{i}",
            "source_type": "atar",
            "persona": "naive_vague",
            "query": f"gold query {i}",
            "expected_code": "6404199000",
        }
        for i in range(1, n + 1)
    ]


def _session_row(i):
    """A minimal successful run_one_session result, enough for run_config to
    count and (against a faked connection) insert it."""
    return {
        "gold_id": i,
        "source_id": f"60000436{i}",
        "persona": "naive_vague",
        "query": "gold query",
        "expected_code": "6404199000",
        "rounds": 1,
        "final_mode": "answers",
        "final_top1": "6404199000",
        "final_set": ["6404199000"],
        "gold_in_final_set": True,
        "gold_in_top1": True,
        "gold_in_top5": True,
        "gold_rank": 1,
        "survivor_set_size": 1,
        "classify_calls": 1,
        "simulator_calls": 1,
        "simulator_failed": False,
        "est_cost_usd": 0.007,
        "latency_seconds": 1.0,
        "config": {},
        "trace_json": {},
    }


async def _run_config_with_session_results(conn, session_results):
    """run_config with everything faked except its own counting/reporting."""
    calls = {"n": 0}

    async def fake_run_one_session(gold, oracle, **kwargs):
        outcome = session_results[calls["n"]]
        calls["n"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with (
        patch(
            "classification_core.run_classify_matrix.run_one_session",
            new=fake_run_one_session,
        ),
        patch(
            "classification_core.run_classify_matrix.build_loo_map",
            return_value={},
        ),
    ):
        return await classify_matrix.run_config(
            conn,
            run_label="test_label",
            strategy="converge",
            prompt_mode="baseline",
            augmentation="facts+kg",
            model="gpt-5-mini",
            candidate_limit=40,
            personas=["naive_vague"],
            limit=None,
            concurrency=4,
            max_rounds=5,
        )


class RunConfigFailureCountingTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_session_failing_is_counted_and_reported_not_dropped(self):
        conn = FakeConn([
            _gold_rows(3),          # the kg.eval_gold select
            [{"id": "600004361", "body": "ruling 1"}],  # the kg.kg_edges oracle select
        ])

        with redirect_stdout(StringIO()) as stdout:
            results, failures = await _run_config_with_session_results(
                conn, [RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")]
            )

        self.assertEqual(results, [])
        self.assertEqual(len(failures), 3)
        self.assertTrue(all(f["failure"] for f in failures))
        self.assertIn("3/3 sessions failed", stdout.getvalue())

    async def test_a_partial_failure_is_counted_alongside_the_successes(self):
        conn = FakeConn([
            _gold_rows(3),
            [{"id": "600004361", "body": "ruling 1"}],
        ])

        with redirect_stdout(StringIO()) as stdout:
            results, failures = await _run_config_with_session_results(
                conn, [RuntimeError("boom"), _session_row(2), _session_row(3)]
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(len(failures), 1)
        self.assertIn("1/3 sessions failed", stdout.getvalue())


class MainExitStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_main_exits_nonzero_when_no_session_produced_a_result(self):
        # All-failure (or empty) work must report itself as unsuccessful, not
        # exit 0 as if a clean run had completed.
        conn = FakeConn([])
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch(
                "classification_core.run_classify_matrix.psycopg.connect",
                return_value=conn,
            ),
            patch(
                "classification_core.run_classify_matrix._ensure_table",
            ),
            patch(
                "classification_core.run_classify_matrix.run_config",
                new=AsyncMock(return_value=([], [{"failure": True, "error": "NameError(...)"}])),
            ),
            patch(
                "classification_core.run_classify_matrix._print_summary",
            ),
            patch("sys.argv", ["run_classify_matrix.py", "--run-label", "test"]),
            redirect_stderr(StringIO()) as stderr,
        ):
            with self.assertRaises(SystemExit) as raised:
                await classify_matrix.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("run unsuccessful", stderr.getvalue())

    async def test_main_exits_cleanly_when_sessions_produced_results(self):
        conn = FakeConn([])
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
            patch(
                "classification_core.run_classify_matrix.psycopg.connect",
                return_value=conn,
            ),
            patch(
                "classification_core.run_classify_matrix._ensure_table",
            ),
            patch(
                "classification_core.run_classify_matrix.run_config",
                new=AsyncMock(return_value=([{"persona": "naive_vague"}], [])),
            ),
            patch(
                "classification_core.run_classify_matrix._print_summary",
            ),
            patch("sys.argv", ["run_classify_matrix.py", "--run-label", "test"]),
        ):
            await classify_matrix.main()  # must not raise SystemExit


if __name__ == "__main__":
    unittest.main()
