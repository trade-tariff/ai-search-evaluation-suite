"""Unit tests for classification_core.qa_loop.simulate_trader_answer's model
selection (AI-1428).

simulator_model was accepted and stored on an evaluation run but never
reached the trader-simulator's actual API call -- every run used the same
hardcoded SIMULATOR_MODEL regardless of what was picked. These tests prove
the model used for one call is whatever that call was explicitly given, not
a shared global two concurrently-executing runs could leak into each other
through.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "product" / "backend"))

from classification_core.qa_loop import SIMULATOR_MODEL, SessionFacts, simulate_trader_answer


class FakeResponse:
    def __init__(self, content: str):
        self.choices = [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]


class FakeCompletions:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.chat = type("Chat", (), {"completions": FakeCompletions(response)})()


_VALID_CHOICE = '{"slot": "material", "choice_index": 0, "reasoning": "matches the oracle text"}'


class SimulateTraderAnswerModelSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_the_explicitly_passed_model_for_the_api_call(self):
        client = FakeClient(FakeResponse(_VALID_CHOICE))

        await simulate_trader_answer(
            client=client, session=SessionFacts(), raw_query="steel pipe",
            question="What is it made of?", options=["Steel", "Plastic"],
            round_number=1, oracle_text="a steel pipe", model="gpt-4o",
        )

        self.assertEqual(client.chat.completions.calls[0]["model"], "gpt-4o")

    async def test_falls_back_to_the_default_simulator_model_when_none_is_passed(self):
        client = FakeClient(FakeResponse(_VALID_CHOICE))

        await simulate_trader_answer(
            client=client, session=SessionFacts(), raw_query="steel pipe",
            question="What is it made of?", options=["Steel", "Plastic"],
            round_number=1, oracle_text="a steel pipe",
        )

        self.assertEqual(client.chat.completions.calls[0]["model"], SIMULATOR_MODEL)
