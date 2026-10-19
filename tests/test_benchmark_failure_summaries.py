"""Provider failures remain visible in benchmark accuracy, coverage and cost."""

import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/product/backend"))

import benchmark
from schemas import AppConfig, BenchmarkRun, CompletionResult, ModelConfig, ReferenceConfig

GOLD = "6404199000"


def completion(prompt=1, error=None, model="candidate", cost=0.2):
    return CompletionResult(model_id=model, prompt_index=prompt, error=error, total_cost=cost,
                            response_type="error" if error else "answers",
                            response_text="" if error else '{"answers":[{"commodity_code":"6404199000"}]}')


class FailureSummaryTest(unittest.TestCase):
    def test_legacy_failed_completion_is_included_in_denominator_and_cost(self):
        good, bad = completion(), completion(2, "provider failed", cost=0.3)
        run = BenchmarkRun(id="fixture", timestamp="fixture", gold_mode=True,
                           model_results=[good, bad],
                           evaluations=[benchmark._evaluate_gold_only(good, GOLD)])
        # A surviving evaluation records this prompt's label for other models.
        other = benchmark._evaluate_gold_only(completion(2, model="other"), GOLD)
        run.evaluations.append(other)
        summary = next(s for s in benchmark.compute_summaries(run, {}) if s.model_id == "candidate")
        self.assertEqual(summary.attempted_count, 2)
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(summary.success_rate, 0.5)
        self.assertEqual(summary.gold_evaluated_count, 2)
        self.assertEqual(summary.gold_top1_rate, 0.5)
        self.assertEqual(summary.total_cost, 0.5)
        self.assertEqual(summary.avg_cost_per_classification, 0.25)
        self.assertEqual(len(run.evaluations), 2, "summary recovery must not mutate the saved evaluations")

    def test_all_failure_model_does_not_disappear(self):
        bad = completion(error="provider failed", cost=0.4)
        run = BenchmarkRun(id="fixture", timestamp="fixture", gold_mode=True, model_results=[bad])
        summary, = benchmark.compute_summaries(run, {})
        self.assertEqual(summary.model_id, "candidate")
        self.assertEqual(summary.attempted_count, 1)
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(summary.success_rate, 0.0)
        self.assertEqual(summary.total_cost, 0.4)
        self.assertIsNone(summary.gold_top1_rate, "unknown historical gold labels must not be guessed")

    def test_failed_reference_calls_keep_their_recorded_cost(self):
        raw = completion(error="reference failed", model="reference", cost=0.4)
        consensus = completion(error="No valid panel results", model="consensus", cost=0.0)
        run = BenchmarkRun(id="fixture", timestamp="fixture", panel_results=[raw], consensus_results=[consensus])
        summary, = benchmark.compute_summaries(run, {})
        self.assertEqual(summary.model_id, "consensus")
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(summary.total_cost, 0.4)


class FailureRecordingTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_gold_run_records_failure_as_a_miss_and_issue(self):
        config = AppConfig(models=[ModelConfig(id="candidate", name="candidate", provider="openai", model_id="fixture")],
                           reference_config=ReferenceConfig(model_id="candidate"))

        async def answer(_provider, _model, prompt, *_args, **_kwargs):
            return completion(prompt, error="provider failed" if prompt == 2 else None, cost=0.2)

        with ExitStack() as stack:
            for name, value in {"get_gold_code": GOLD, "get_gold_facts": [], "get_oracle_text": None,
                                "gold_is_retrievable": True, "get_provider": None}.items():
                stack.enter_context(patch.object(benchmark, name, return_value=value))
            stack.enter_context(patch.object(benchmark, "_run_qa_loop", new=AsyncMock(side_effect=answer)))
            stack.enter_context(patch.object(benchmark, "_save_run"))
            for name, value in {"_current_run": None, "_cancel_event": None, "_current_model_map": {}}.items():
                stack.enter_context(patch.object(benchmark, name, value))
            events = [event async for event in benchmark.run_benchmark([1, 2], ["candidate"], config, gold_mode=True)]
            run = benchmark.get_current_run()
            self.assertEqual(len(run.evaluations), 2)
            failed, = [ev for ev in run.evaluations if ev.error]
            self.assertEqual(failed.prompt_index, 2)
            self.assertFalse(failed.gold_top1_match)
            self.assertEqual(run.summaries[0].gold_top1_rate, 0.5)
            self.assertEqual(run.summaries[0].failed_count, 1)
            self.assertEqual(run.summaries[0].total_cost, 0.4)
            self.assertEqual(run.issues[0]["source"], "candidate")
            self.assertIn("task:failed", [event.event for event in events])
