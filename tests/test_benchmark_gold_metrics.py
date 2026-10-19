"""Gold ranking fields survive evaluation construction and summarisation."""

import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/product/backend"))

import benchmark
from judge import compute_gold_metrics, evaluate_pair
from schemas import AppConfig, BenchmarkRun, CompletionResult, ModelConfig, ReferenceConfig

GOLD = "6404199000"


def completion(codes, model="candidate", prompt=1):
    return CompletionResult(
        model_id=model,
        prompt_index=prompt,
        response_type="answers",
        response_text=json.dumps({"answers": [
            {"commodity_code": code, "confidence": "Strong"} for code in codes
        ]}),
    )


class GoldMetricConstructionTest(unittest.TestCase):
    def test_gold_only_and_reference_scoring_preserve_all_gold_fields(self):
        baseline = completion([GOLD], "reference")
        cases = ([GOLD], ["0101210000", "0201100000", GOLD], ["0101210000"])
        for codes in cases:
            for gold in (GOLD, None):
                with self.subTest(codes=codes, gold=gold):
                    target = completion(codes)
                    expected = compute_gold_metrics(list(codes), gold)
                    for evaluation in (
                        benchmark._evaluate_gold_only(target, gold),
                        evaluate_pair(baseline, target, gold),
                    ):
                        for field, value in expected.items():
                            self.assertEqual(getattr(evaluation, field), value, field)

    def test_perfect_answer_has_perfect_rank_summary(self):
        evaluation = benchmark._evaluate_gold_only(completion([GOLD]), GOLD)
        run = BenchmarkRun(id="fixture", timestamp="fixture", evaluations=[evaluation])
        summary, = benchmark.compute_summaries(run, {})
        self.assertEqual(summary.gold_top3_rate, 1.0)
        self.assertEqual(summary.gold_top5_rate, 1.0)
        self.assertEqual(summary.avg_gold_reciprocal_rank, 1.0)


class ConsensusGoldMetricTest(unittest.IsolatedAsyncioTestCase):
    async def test_reference_self_evaluation_preserves_rank_metrics(self):
        models = [ModelConfig(id=name, name=name, provider="openai", model_id=name)
                  for name in ("reference", "candidate")]
        config = AppConfig(models=models, reference_config=ReferenceConfig(model_id="reference"))

        async def answer(_provider, model, prompt, *_args, **_kwargs):
            return completion(["0101210000", GOLD], model.id, prompt)

        with ExitStack() as stack:
            for name, value in {
                "get_gold_code": GOLD,
                "get_gold_facts": [],
                "get_oracle_text": None,
                "gold_is_retrievable": True,
                "get_provider": None,
            }.items():
                stack.enter_context(patch.object(benchmark, name, return_value=value))
            stack.enter_context(patch.object(benchmark, "_run_qa_loop", new=AsyncMock(side_effect=answer)))
            stack.enter_context(patch.object(benchmark, "_save_run"))
            stack.enter_context(patch.object(benchmark, "_current_run", None))
            stack.enter_context(patch.object(benchmark, "_current_model_map", {}))
            stack.enter_context(patch.object(benchmark, "_cancel_event", None))
            events = [event async for event in benchmark.run_benchmark([1], ["candidate"], config, gold_mode=False)]
            self.assertEqual(events[-1].event, "benchmark:complete")
            run = benchmark.get_current_run()
            self.assertEqual(len(run.evaluations), 2)
            for evaluation in run.evaluations:
                self.assertTrue(evaluation.gold_top3_hit)
                self.assertTrue(evaluation.gold_top5_hit)
                self.assertEqual(evaluation.gold_reciprocal_rank, 0.5)
            for summary in run.summaries:
                self.assertEqual(summary.gold_top3_rate, 1.0)
                self.assertEqual(summary.gold_top5_rate, 1.0)
                self.assertEqual(summary.avg_gold_reciprocal_rank, 0.5)
