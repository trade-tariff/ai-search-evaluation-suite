"""Offline coverage for the shared trader simulator and per-session facts."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class SessionFactsTest(unittest.TestCase):
    def test_sessions_and_snapshot_mappings_are_independent(self):
        session = SessionFacts()
        other_session = SessionFacts()
        session.record("origin", "UK", "Where was it made?", 1)

        snapshot = session.snapshot()
        snapshot.clear()

        self.assertEqual(other_session.snapshot(), {})
        self.assertEqual(session.get("origin").answer, "UK")
        self.assertEqual(session.as_qa_history(), [
            {"question": "Where was it made?", "answer": "UK"},
        ])


class SimulateTraderAnswerBehaviourTest(unittest.IsolatedAsyncioTestCase):
    async def test_commits_the_exact_option_and_includes_oracle_and_known_facts(self):
        client = FakeClient(FakeResponse(_VALID_CHOICE))
        session = SessionFacts()
        session.record("origin", "UK", "Where was it made?", 1)

        result = await simulate_trader_answer(
            client=client, session=session, raw_query="pipe",
            question="What is it made of?", options=["Steel", "Plastic"],
            round_number=2, oracle_text="a steel pipe",
        )

        self.assertEqual(result["chosen"], "Steel")
        self.assertEqual(result["choice_index"], 0)
        self.assertFalse(result["simulator_failed"])
        self.assertEqual(result["attempts"], 1)
        self.assertIsNone(result["last_error"])
        fact = session.get("material")
        self.assertEqual((fact.answer, fact.source_question, fact.round_number),
                         ("Steel", "What is it made of?", 2))
        self.assertEqual(session.get("origin").answer, "UK")
        call = client.chat.completions.calls[0]
        self.assertEqual(call["response_format"], {"type": "json_object"})
        prompt = call["messages"][1]["content"]
        self.assertIn("Oracle (authoritative product description)\na steel pipe", prompt)
        self.assertIn('slot="origin" answer="UK"', prompt)
        self.assertIn("[0] Steel\n[1] Plastic", prompt)

    async def test_empty_options_do_not_call_the_provider_or_commit_a_fact(self):
        client = FakeClient(FakeResponse(_VALID_CHOICE))
        session = SessionFacts()

        result = await simulate_trader_answer(
            client=client, session=session, raw_query="pipe",
            question="What is it made of?", options=[], round_number=1,
        )

        self.assertTrue(result["simulator_failed"])
        self.assertIsNone(result["chosen"])
        self.assertIsNone(result["choice_index"])
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(client.chat.completions.calls, [])
        self.assertEqual(session.snapshot(), {})

    async def test_retry_commits_only_the_valid_choice_with_a_default_slot(self):
        client = FakeClient(FakeResponse(_VALID_CHOICE))
        session = SessionFacts()
        responses = [
            FakeResponse('{"slot": "bad", "choice_index": 99}'),
            FakeResponse('{"choice_index": "1"}'),
        ]

        with patch.object(client.chat.completions, "create", new=AsyncMock(side_effect=responses)) as create:
            result = await simulate_trader_answer(
                client=client, session=session, raw_query="pipe",
                question="What is it made of?", options=["Steel", "Plastic"],
                round_number=2,
            )

        self.assertFalse(result["simulator_failed"])
        self.assertEqual((result["chosen"], result["choice_index"], result["attempts"]),
                         ("Plastic", 1, 2))
        self.assertEqual(result["slot"], "round_2")
        self.assertEqual(set(session.snapshot()), {"round_2"})
        self.assertEqual(create.await_count, 2)
        self.assertIn("Your previous attempt failed:",
                      create.await_args_list[1].kwargs["messages"][1]["content"])

    async def test_invalid_choices_exhaust_retries_without_changing_facts(self):
        for content in ("not json", '{"choice_index": true}', '{"choice_index": 2}'):
            with self.subTest(content=content):
                client = FakeClient(FakeResponse(content))
                session = SessionFacts()
                session.record("origin", "UK", "Where was it made?", 1)
                before = session.snapshot()

                result = await simulate_trader_answer(
                    client=client, session=session, raw_query="pipe",
                    question="What is it made of?", options=["Steel", "Plastic"],
                    round_number=2,
                )

                self.assertTrue(result["simulator_failed"])
                self.assertIsNone(result["chosen"])
                self.assertIsNone(result["choice_index"])
                self.assertEqual(result["attempts"], 3)
                self.assertEqual(len(client.chat.completions.calls), 3)
                self.assertTrue(result["last_error"])
                self.assertEqual(session.snapshot(), before)

    async def test_provider_errors_exhaust_retries_without_a_fabricated_answer(self):
        client = FakeClient(FakeResponse(_VALID_CHOICE))
        session = SessionFacts()
        with patch.object(client.chat.completions, "create", new=AsyncMock(side_effect=RuntimeError("offline failure"))) as create:
            result = await simulate_trader_answer(
                client=client, session=session, raw_query="pipe",
                question="What is it made of?", options=["Steel", "Plastic"],
                round_number=1,
            )

        self.assertTrue(result["simulator_failed"])
        self.assertIsNone(result["chosen"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(create.await_count, 3)
        self.assertIn("API call failed:", result["last_error"])
        self.assertEqual(session.snapshot(), {})
