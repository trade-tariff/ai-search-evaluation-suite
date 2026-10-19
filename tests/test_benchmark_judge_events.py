"""Judge completions received during fan-out must survive until scoring."""

import asyncio
import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/product/backend"))

import benchmark
from schemas import ApiKeys, AppConfig, CompletionResult, ModelConfig, ReferenceConfig, SimulatorConfig


class JudgeEventTest(unittest.IsolatedAsyncioTestCase):
    async def test_judge_finishing_before_next_candidate_is_not_lost(self):
        release_second = asyncio.Event()

        class EventQueue(asyncio.Queue):
            async def get(self):
                item = await super().get()
                if item[0] == "judge_done":
                    release_second.set()
                return item

        event_queue = EventQueue()
        models = [ModelConfig(id=name, name=name, provider="openai", model_id=name)
                  for name in ("reference", "candidate")]
        config = AppConfig(
            models=models,
            api_keys=ApiKeys(openai="offline-fixture"),
            simulator_config=SimulatorConfig(enabled=False),
            reference_config=ReferenceConfig(model_id="reference"),
        )

        async def answer(_provider, model, prompt, *_args, **_kwargs):
            if model.id == "candidate" and prompt == 2:
                await release_second.wait()
            return CompletionResult(model_id=model.id, prompt_index=prompt, response_type="answers",
                                    response_text=json.dumps({"answers": [{"commodity_code": "6404199000"}]}))

        with ExitStack() as stack:
            for name, value in {
                "get_gold_code": None, "get_gold_facts": [], "get_oracle_text": None,
                "gold_is_retrievable": None, "get_provider": None, "get_raw_query": "fixture",
            }.items():
                stack.enter_context(patch.object(benchmark, name, return_value=value))
            stack.enter_context(patch.object(benchmark, "_run_qa_loop", new=AsyncMock(side_effect=answer)))
            judge = stack.enter_context(patch.object(benchmark, "judge_response", new=AsyncMock(return_value={
                "fact_consistency": 8.0, "question_quality": 7.0, "reasoning": "fixture", "error": False,
            })))
            stack.enter_context(patch("openai.AsyncOpenAI", return_value=object()))
            stack.enter_context(patch.object(benchmark.asyncio, "Queue", return_value=event_queue))
            stack.enter_context(patch.object(benchmark, "JUDGE_DRAIN_TIMEOUT_S", 0.05))
            stack.enter_context(patch.object(benchmark, "_save_run"))
            for name, value in {"_current_run": None, "_cancel_event": None, "_current_model_map": {}}.items():
                stack.enter_context(patch.object(benchmark, name, value))

            events = [event async for event in benchmark.run_benchmark([1, 2], ["candidate"], config, gold_mode=False)]
            run = benchmark.get_current_run()
            candidates = [e for e in run.evaluations if e.model_id == "candidate"]
            self.assertEqual(judge.await_count, 2)
            self.assertEqual(len(candidates), 2)
            self.assertTrue(release_second.is_set())
            for evaluation in candidates:
                self.assertFalse(evaluation.judge_error)
                self.assertEqual(evaluation.judge_fact_consistency, 8.0)
            self.assertEqual(run.issues, [])
            self.assertEqual(events[-1].event, "benchmark:complete")
