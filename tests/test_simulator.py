"""Unit tests for the trader simulator's deterministic behaviour.

These tests do not call a provider. They prove option matching, fact-store
consistency, and the failure fallback that the Q&A loop depends on.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from fact_store import Fact, FactStore
from schemas import SimulatorConfig
from simulator import _format_facts, _match_to_options, _tidy, simulate_answer


def _config() -> SimulatorConfig:
    return SimulatorConfig(
        model="gpt-5-nano",
        reasoning_effort="low",
        temperature=0.0,
        input_cost_per_million=1.0,
        output_cost_per_million=2.0,
    )


class FakeResponse:
    def __init__(self, content: str, prompt_tokens: int = 100, completion_tokens: int = 20):
        self.choices = [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]
        self.usage = type("Usage", (), {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens})()


class FakeCompletions:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responder(kwargs)
        if isinstance(result, Exception):
            raise result
        return result


class FakeClient:
    def __init__(self, responder):
        self.chat = type("Chat", (), {"completions": FakeCompletions(responder)})()


class SimulatorHelpersTest(unittest.TestCase):
    def test_tidy_strips_wrapping_punctuation_and_case(self):
        self.assertEqual(_tidy("  Powder. "), "powder")
        self.assertEqual(_tidy('"Liquid,"'), "liquid")

    def test_match_to_options_prefers_exact_then_normalised_then_substring(self):
        options = ["Straight lengths", "Powder", "Liquid"]
        self.assertEqual(_match_to_options("Powder", options), "Powder")
        self.assertEqual(_match_to_options("powder.", options), "Powder")
        self.assertEqual(_match_to_options("fine powder", options), "Powder")
        self.assertIsNone(_match_to_options("", options))
        self.assertIsNone(_match_to_options("gas", options))

    def test_format_facts_renders_prior_commitments(self):
        self.assertIn("no prior commitments", _format_facts({}))
        rendered = _format_facts({
            "form": Fact("form", "Powder", "What form?", "model-a", 1),
        })
        self.assertIn('slot="form"', rendered)
        self.assertIn('answer="Powder"', rendered)
        self.assertIn("What form?", rendered)


class SimulateAnswerTest(unittest.IsolatedAsyncioTestCase):
    async def test_empty_options_does_not_call_the_provider(self):
        client = FakeClient(lambda _kwargs: FakeResponse("{}"))
        result = await simulate_answer(
            client, FactStore(), 0, 1, "model-a", "wheat flour", "What form?", [], _config(),
        )
        self.assertEqual(result["chosen"], "Yes")
        self.assertEqual(result["slot"], "unanswerable")
        self.assertEqual(client.chat.completions.calls, [])

    async def test_new_slot_is_recorded_and_costed(self):
        store = FactStore()
        client = FakeClient(lambda _kwargs: FakeResponse(
            '{"slot":"form","chosen":"powder","consistent_with_prior":false,"reasoning":"common form"}'
        ))
        result = await simulate_answer(
            client,
            store,
            3,
            1,
            "model-a",
            "wheat flour",
            "What form is it?",
            ["Powder", "Liquid"],
            _config(),
        )
        self.assertEqual(result["chosen"], "Powder")
        self.assertEqual(result["slot"], "form")
        self.assertFalse(result["consistent_with_prior"])
        self.assertEqual(result["cost"], 0.00014)
        self.assertEqual(store.get(3, "form").answer, "Powder")
        commits, _cursor = store.drain_new_commits(0)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["slot"], "form")

    async def test_existing_slot_wins_over_a_contradicting_model_choice(self):
        store = FactStore()
        store.record(3, "form", "Liquid", "What form?", "model-a", 1)
        client = FakeClient(lambda _kwargs: FakeResponse(
            '{"slot":"form","chosen":"Powder","consistent_with_prior":false,"reasoning":"ignored"}'
        ))
        result = await simulate_answer(
            client,
            store,
            3,
            2,
            "model-b",
            "wheat flour",
            "What form is it in?",
            ["Powder", "Liquid"],
            _config(),
        )
        self.assertEqual(result["chosen"], "Liquid")
        self.assertTrue(result["consistent_with_prior"])
        self.assertEqual(store.get(3, "form").answer, "Liquid")

    async def test_fenced_json_and_oracle_are_accepted(self):
        seen = {}

        def responder(kwargs):
            seen.update(kwargs)
            return FakeResponse(
                "```json\n"
                '{"slot":"intended_use","chosen":"Food","consistent_with_prior":false,"reasoning":"oracle says food"}\n'
                "```"
            )

        oracle = "This product is food. " + ("x" * 7000)
        result = await simulate_answer(
            FakeClient(responder),
            FactStore(),
            1,
            1,
            "model-a",
            "flour",
            "What is the intended use?",
            ["Food", "Industrial"],
            _config(),
            oracle_text=oracle,
        )
        self.assertEqual(result["chosen"], "Food")
        self.assertEqual(result["slot"], "intended_use")
        user_prompt = seen["messages"][1]["content"]
        self.assertIn("Oracle", user_prompt)
        self.assertIn("[... truncated ...]", user_prompt)
        self.assertLess(len(user_prompt), 8000)
        self.assertIn("reasoning_effort", seen)
        self.assertNotIn("temperature", seen)

    async def test_provider_failure_picks_the_first_option_and_does_not_record_a_fact(self):
        store = FactStore()
        client = FakeClient(lambda _kwargs: ValueError("schema rejected"))
        result = await simulate_answer(
            client,
            store,
            4,
            1,
            "model-a",
            "bolts",
            "What material?",
            ["Steel", "Brass"],
            _config(),
        )
        self.assertEqual(result["chosen"], "Steel")
        self.assertEqual(result["slot"], "_error_fallback")
        self.assertIsNone(store.get(4, "_error_fallback"))
        self.assertEqual(result["cost"], 0.0)

    async def test_unparseable_json_falls_back_to_the_first_option(self):
        store = FactStore()
        result = await simulate_answer(
            FakeClient(lambda _kwargs: FakeResponse("not json")),
            store,
            5,
            1,
            "model-a",
            "bolts",
            "What material?",
            ["Steel", "Brass"],
            _config(),
        )
        self.assertEqual(result["chosen"], "Steel")
        self.assertEqual(result["slot"], "_parse_error")
        self.assertEqual(store.get(5, "_parse_error").answer, "Steel")


if __name__ == "__main__":
    unittest.main()
