"""Closing a benchmark response also closes its owned provider tasks."""

import asyncio
import sys
import unittest

import anyio

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/product/backend"))

import benchmark
import main
from schemas import AppConfig, BenchmarkRequest, CompletionResult, ModelConfig, ReferenceConfig


class BenchmarkDisconnectTest(unittest.IsolatedAsyncioTestCase):
    def fixtures(self, answer):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in {
            "get_gold_code": "6404199000", "get_gold_facts": [], "get_oracle_text": None,
            "gold_is_retrievable": True, "get_provider": None,
        }.items():
            stack.enter_context(patch.object(benchmark, name, return_value=value))
        stack.enter_context(patch.object(benchmark, "_run_qa_loop", new=AsyncMock(side_effect=answer)))
        saved = stack.enter_context(patch.object(benchmark, "_save_run"))
        for name, value in {"_current_run": None, "_cancel_event": None, "_current_model_map": {}}.items():
            stack.enter_context(patch.object(benchmark, name, value))
        config = AppConfig(
            models=[ModelConfig(id="fixture", name="fixture", model_id="fixture", provider="openai")],
            reference_config=ReferenceConfig(model_id="fixture"),
        )
        return stack, config, saved

    async def test_response_disconnect_cancels_and_awaits_all_children(self):
        tasks = []
        exited = set()
        both_started = asyncio.Event()

        async def blocked(_provider, _model, prompt, *_args, **_kwargs):
            tasks.append(asyncio.current_task())
            if len(tasks) == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.add(prompt)

        stack, config, saved = self.fixtures(blocked)
        stack.enter_context(patch.object(main, "_require_workbench_spend"))
        stack.enter_context(patch.object(main, "load_config", return_value=config))
        response = await main.api_start_benchmark(BenchmarkRequest(prompt_indices=[1, 2], model_ids=["fixture"], gold_mode=True))
        stream = response.body_iterator
        async for frame in stream:
            if "event: fanout:start\n" in frame:
                break
        await asyncio.wait_for(both_started.wait(), 1)
        await stream.aclose()

        self.assertEqual(exited, {1, 2})
        self.assertTrue(all(task.done() for task in tasks))
        self.assertEqual(benchmark.get_current_run().status, "interrupted")
        self.assertFalse(benchmark.is_run_active())
        self.assertIsNone(benchmark._cancel_event)
        self.assertEqual(saved.call_args.args[0].status, "interrupted")

    async def test_cancelled_http_scope_finishes_cleanup_and_checkpoint(self):
        started = asyncio.Event()
        exited = asyncio.Event()

        async def blocked(*_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        stack, config, saved = self.fixtures(blocked)
        stack.enter_context(patch.object(main, "_require_workbench_spend"))
        stack.enter_context(patch.object(main, "load_config", return_value=config))
        response = await main.api_start_benchmark(BenchmarkRequest(
            prompt_indices=[1], model_ids=["fixture"], gold_mode=True,
        ))

        async def consume():
            async for _frame in response.body_iterator:
                pass

        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await asyncio.wait_for(started.wait(), 1)
            group.cancel_scope.cancel()

        self.assertTrue(exited.is_set())
        self.assertEqual(benchmark.get_current_run().status, "interrupted")
        self.assertFalse(benchmark.is_run_active())
        self.assertIsNone(benchmark._cancel_event)
        self.assertEqual(saved.call_args.args[0].status, "interrupted")

    async def test_normal_completion_keeps_its_terminal_status(self):
        async def answer(_provider, model, prompt, *_args, **_kwargs):
            return CompletionResult(model_id=model.id, prompt_index=prompt, response_type="answers",
                                    response_text='{"answers":[{"commodity_code":"6404199000"}]}')

        _stack, config, _saved = self.fixtures(answer)
        events = [event async for event in benchmark.run_benchmark([1], ["fixture"], config, gold_mode=True)]
        self.assertEqual(events[-1].event, "benchmark:complete")
        self.assertEqual(benchmark.get_current_run().status, "complete")
        self.assertIsNone(benchmark._cancel_event)

    async def test_second_stream_cannot_replace_an_active_run(self):
        async def blocked(*_args, **_kwargs):
            await asyncio.Event().wait()

        _stack, config, _saved = self.fixtures(blocked)
        first = benchmark.run_benchmark([1], ["fixture"], config, gold_mode=True)
        await anext(first)
        first_run = benchmark.get_current_run()
        second = [event async for event in benchmark.run_benchmark([1], ["fixture"], config, gold_mode=True)]
        self.assertEqual([event.event for event in second], ["error"])
        self.assertIs(benchmark.get_current_run(), first_run)
        await first.aclose()
        self.assertEqual(first_run.status, "interrupted")
