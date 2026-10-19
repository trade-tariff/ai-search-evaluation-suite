"""Measurement-isolation regression tests for eval session-fact persistence.

Pins that the eval runners (run_classify_matrix, run_qna_mode_comparison) do
not write session facts to the shared classification/qa KG scopes by default,
and that --persist-session-facts opts in to the persist call. Trace and result
recording is unchanged.

No database, network, or provider calls: run_qa_session and
persist_session_facts_to_kg are patched, and the psycopg connection is faked.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

import classification_core.qa_loop as qa_loop
import classification_core.run_classify_matrix as classify_matrix
import classification_core.run_qna_mode_comparison as qna_mode_comparison

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


class SessionFactsPersistSkippedTest(unittest.TestCase):
    def test_the_skipped_summary_reports_opted_out_with_nothing_persisted(self):
        summary = qa_loop.session_facts_persist_skipped([{"key": "material"}])

        self.assertEqual(
            summary,
            {"opt_in": False, "attempted": 1, "persisted": 0, "skipped": 1, "ids": []},
        )

    def test_the_skipped_summary_handles_no_facts(self):
        self.assertEqual(qa_loop.session_facts_persist_skipped([])["attempted"], 0)
        self.assertEqual(qa_loop.session_facts_persist_skipped(None)["attempted"], 0)


class RunClassifyMatrixIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_session_facts_are_not_persisted_to_the_shared_kg_by_default(self):
        with (
            patch(
                "classification_core.qa_loop.run_qa_session",
                new=AsyncMock(return_value=_qa_session_result()),
            ),
            patch(
                "classification_core.qa_loop.persist_session_facts_to_kg",
            ) as mocked_persist,
        ):
            result = await classify_matrix.run_one_session(
                ATAR_GOLD, "ruling text", strategy="converge", prompt_mode="baseline",
                augmentation="facts+kg", model="gpt-5-mini",
                candidate_limit=40, max_rounds=5,
            )

        mocked_persist.assert_not_called()
        summary = result["trace_json"]["persisted_session_facts"]
        self.assertEqual(summary["opt_in"], False)
        self.assertEqual(summary["persisted"], 0)
        self.assertEqual(summary["skipped"], 1)
        # The rest of the result recording is unchanged.
        self.assertEqual(result["gold_in_final_set"], True)
        self.assertEqual(result["gold_in_top1"], True)

    async def test_opting_in_persists_session_facts_as_before(self):
        # Executes the real oracle-backed simulator authority rule; only the
        # provider/DB work is stubbed.
        with (
            patch(
                "classification_core.qa_loop.run_qa_session",
                new=AsyncMock(return_value=_qa_session_result()),
            ),
            patch(
                "classification_core.qa_loop.persist_session_facts_to_kg",
                return_value={"available": True, "persisted": 1, "ids": [77]},
            ) as mocked_persist,
        ):
            result = await classify_matrix.run_one_session(
                ATAR_GOLD, "ruling text", strategy="converge", prompt_mode="baseline",
                augmentation="facts+kg", model="gpt-5-mini",
                candidate_limit=40, max_rounds=5,
                persist_session_facts=True,
            )

        mocked_persist.assert_called_once()
        self.assertEqual(mocked_persist.call_args.kwargs["commodity_code"], "6404199000")
        self.assertEqual(mocked_persist.call_args.kwargs["authority_tier"], 5)
        self.assertEqual(
            mocked_persist.call_args.kwargs["provenance"]["source_authority_tier"], 5
        )
        self.assertEqual(
            result["trace_json"]["persisted_session_facts"], {"available": True, "persisted": 1, "ids": [77]}
        )

    async def test_opted_in_fallback_facts_stay_low_authority_for_both_strategies(self):
        candidate = {"commodity_code": "6404199000"}
        turn = {
            "mode": "questions",
            "question": {"question": "Material?", "options": ["Textile", "Leather"]},
            "candidates": [candidate], "answers": [candidate],
        }
        for strategy in ("converge", "eliminate"):
            for provider_calls, oracle in (("0", "ruling text"), ("1", "")):
                with self.subTest(strategy=strategy, provider_calls=provider_calls, oracle=oracle):
                    with (
                        patch.dict(os.environ, {
                            "CLASSIFICATION_ALLOW_PROVIDER_CALLS": provider_calls,
                            "OPENAI_API_KEY": "test-key",
                        }),
                        patch.object(qa_loop.classification, "classify_step", return_value=turn),
                        patch.object(qa_loop.classification, "initial_candidates_for_eliminate",
                                     return_value=([candidate], [candidate])),
                        patch.object(qa_loop.classification, "eliminate_step", return_value=turn),
                        patch.object(qa_loop, "simulate_trader_answer", new=AsyncMock()) as simulator,
                        patch.object(qa_loop, "persist_session_facts_to_kg", return_value={}) as persist,
                    ):
                        result = await classify_matrix.run_one_session(
                            ATAR_GOLD, oracle, strategy=strategy, prompt_mode="baseline",
                            augmentation="facts+kg", model="gpt-5-mini",
                            candidate_limit=40, max_rounds=1, persist_session_facts=True,
                        )
                    simulator.assert_not_awaited()
                    persist.assert_called_once()
                    self.assertEqual(persist.call_args.kwargs["authority_tier"], 8)
                    self.assertEqual(persist.call_args.kwargs["provenance"]["source_authority_tier"], 8)
                    self.assertEqual(result["trace_json"]["rounds"][0]["answer_source"], "fallback_first_option")
                    self.assertTrue(persist.call_args.kwargs["facts"])

    async def test_the_cli_flag_defaults_off_and_passes_through_when_set(self):
        # main() plumbs the flag into run_config; both directions are pinned
        # by capturing run_config's kwargs.
        for argv, expected in (
            (["run_classify_matrix.py", "--run-label", "isolation"], False),
            (["run_classify_matrix.py", "--run-label", "isolation", "--persist-session-facts"], True),
        ):
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
                patch("classification_core.run_classify_matrix.psycopg.connect"),
                patch("classification_core.run_classify_matrix._ensure_table"),
                patch(
                    "classification_core.run_classify_matrix.run_config",
                    new=AsyncMock(side_effect=RuntimeError("configuration captured")),
                ) as mocked_run_config,
                patch("classification_core.run_classify_matrix._print_summary"),
                patch("sys.argv", argv),
            ):
                # Stop at the configuration boundary, independent of result aggregation.
                with self.assertRaisesRegex(RuntimeError, "configuration captured"):
                    await classify_matrix.main()

            self.assertEqual(
                mocked_run_config.call_args.kwargs["persist_session_facts"], expected,
                f"expected persist_session_facts={expected} for argv={argv}",
            )


def _eval_ctx(persist_session_facts=None):
    kwargs = {
        "run_label": "isolation",
        "retrieval_run_label": "retrieval",
        "retrieval_config": {},
        "api_key": None,
        "retrieval_limit": 10,
        "hydrate_limit": 10,
        "max_rounds": 2,
        "question_mode": "facet_rules",
        "question_model": "gpt-5-mini",
        "staging_prompt_mode": "baseline",
        "answerer": "gold_option",
        "allow_spend": False,
        "simulator_client": None,
        "answer_cache": {},
    }
    if persist_session_facts is not None:
        kwargs["persist_session_facts"] = persist_session_facts
    return qna_mode_comparison.EvalContext(**kwargs)


class RunQnaModeComparisonIsolationTest(unittest.TestCase):
    def test_the_eval_context_defaults_to_not_persisting(self):
        self.assertFalse(_eval_ctx().persist_session_facts)

    def test_the_gating_helper_skips_the_shared_kg_write_by_default(self):
        facts = [{"key": "material", "value": "textile"}]
        ctx = _eval_ctx(persist_session_facts=False)

        with patch("classification_core.qa_loop.persist_session_facts_to_kg") as mocked_persist:
            summary = qna_mode_comparison._persist_session_facts_opted_in(
                ctx=ctx, commodity_code="6404199000", facts=facts,
                source="qna_session:e2e:isolation:facet_rules:12",
                provenance={"runner": "run_qna_mode_comparison"},
                authority_tier=5,
            )

        mocked_persist.assert_not_called()
        self.assertEqual(summary["opt_in"], False)
        self.assertEqual(summary["persisted"], 0)
        self.assertEqual(summary["skipped"], 1)

    def test_opting_in_persists_through_the_shared_writer_as_before(self):
        facts = [{"key": "material", "value": "textile"}]
        ctx = _eval_ctx(persist_session_facts=True)

        with patch(
            "classification_core.qa_loop.persist_session_facts_to_kg",
            return_value={"available": True, "persisted": 1, "ids": [78]},
        ) as mocked_persist:
            summary = qna_mode_comparison._persist_session_facts_opted_in(
                ctx=ctx, commodity_code="6404199000", facts=facts,
                source="qna_session:e2e:isolation:facet_rules:12",
                provenance={"runner": "run_qna_mode_comparison"},
                authority_tier=5,
            )

        mocked_persist.assert_called_once_with(
            commodity_code="6404199000",
            facts=facts,
            source="qna_session:e2e:isolation:facet_rules:12",
            provenance={"runner": "run_qna_mode_comparison"},
            authority_tier=5,
        )
        self.assertEqual(summary, {"available": True, "persisted": 1, "ids": [78]})


class ComparisonOracleAuthorityTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_modes_keep_cached_answers_without_oracles_at_low_authority(self):
        import main as product_main

        options = ["Textile", "Leather"]
        candidates = [{"commodity_code": code} for code in ("6404199000", "6403999000")]
        hint = {"question": "Material?", "options": options}
        hydration = {
            "qa_state": {"in_scope_codes": [item["commodity_code"] for item in candidates]},
            "question_hint": hint,
        }
        turn = {"mode": "questions", "question": hint, "answers": candidates}
        for evaluate in (qna_mode_comparison._evaluate_hydration_mode,
                         qna_mode_comparison._evaluate_staging_eliminate):
            for oracle, expected_tier in ((None, 8), ("", 8), ("   ", 8), ("ruling text", 5)):
                with self.subTest(mode=evaluate.__name__, oracle=oracle):
                    gold = {**ATAR_GOLD, "oracle_text": oracle}
                    ctx = _eval_ctx(persist_session_facts=True)
                    ctx.answerer = "llm_trader"
                    ctx.max_rounds = 1
                    ctx.answer_cache[qna_mode_comparison._answer_cache_key(gold["id"], options)] = {
                        "answer": "Textile", "slot": "material",
                    }
                    with (
                        patch.object(product_main, "api_hydration_candidates",
                                     new=AsyncMock(return_value=hydration)),
                        patch.object(qna_mode_comparison.classification, "eliminate_step", return_value=turn),
                        patch.object(qa_loop, "persist_session_facts_to_kg", return_value={}) as persist,
                    ):
                        result = await evaluate(gold, ctx, candidates)
                    persist.assert_called_once()
                    self.assertEqual(persist.call_args.kwargs["authority_tier"], expected_tier)
                    self.assertEqual(persist.call_args.kwargs["provenance"]["source_authority_tier"],
                                     expected_tier)
                    self.assertTrue(persist.call_args.kwargs["facts"])
                    self.assertEqual(result["question_trace"][0]["answer_debug"]["source"], "answer_cache")


if __name__ == "__main__":
    unittest.main()
