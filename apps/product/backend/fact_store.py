"""Per-prompt, per-run fact store.

Problem this solves
-------------------
Different models ask the same concept in different words ("what form?", "what
shape?", "in what state?"). The sha256 cache in simulator.py keys on exact text,
so it never hits in practice. Two models that rationally abstain both fall back
to options[0], but because their option orderings differ, they land on
different final codes - apples-to-oranges again.

This module makes the benchmark consistent at the semantic level: when any
model commits to a fact for a prompt (e.g. form="straight lengths"), every
later model asking about the same concept sees that fact and must be
consistent with it. The slot label is coined dynamically by the simulator
itself (gpt-5.4 low), so the vocabulary grows organically per prompt.

Scope
-----
Per-prompt, per-run. Each prompt_index gets a clean fact store that persists
across all models running that prompt. Cleared between prompts so each query
is judged independently.

Concurrency
-----------
One asyncio.Lock per prompt_index. Simulator calls for the same prompt
serialise through it, so model A committing a fact is visible to model B's
next call. Sibling questions in the same round (resolved via asyncio.gather)
also serialise under this lock - which is correct: we do not want two
concurrent calls to invent two different values for the same slot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Fact:
    slot: str
    answer: str
    source_question: str
    source_model: str
    source_round: int


@dataclass
class PromptFacts:
    # slot -> Fact
    facts: dict[str, Fact] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class FactStore:
    """Per-run, per-prompt fact store with a lock per prompt."""

    def __init__(self) -> None:
        self._by_prompt: dict[int, PromptFacts] = {}
        # Guards creation of per-prompt entries (NOT the per-prompt lock itself).
        self._init_lock = asyncio.Lock()
        # Append-only log of every commit for real-time SSE streaming.
        # run_benchmark drains this between task completions.
        self._commit_log: list[dict] = []

    def ensure_sync(self, prompt_index: int) -> PromptFacts:
        """Create the per-prompt entry if missing. Safe under contention via
        dict.setdefault (atomic on built-in dict). Callers that need a lock
        should use lock_for() instead.
        """
        return self._by_prompt.setdefault(prompt_index, PromptFacts())

    async def _ensure(self, prompt_index: int) -> PromptFacts:
        if prompt_index in self._by_prompt:
            return self._by_prompt[prompt_index]
        async with self._init_lock:
            if prompt_index not in self._by_prompt:
                self._by_prompt[prompt_index] = PromptFacts()
            return self._by_prompt[prompt_index]

    async def lock_for(self, prompt_index: int) -> asyncio.Lock:
        pf = await self._ensure(prompt_index)
        return pf.lock

    def snapshot(self, prompt_index: int) -> dict[str, Fact]:
        pf = self._by_prompt.get(prompt_index)
        if not pf:
            return {}
        # Shallow copy so callers can iterate without holding the lock.
        return dict(pf.facts)

    def get(self, prompt_index: int, slot: str) -> Fact | None:
        pf = self._by_prompt.get(prompt_index)
        if not pf:
            return None
        return pf.facts.get(slot)

    def record(
        self,
        prompt_index: int,
        slot: str,
        answer: str,
        source_question: str,
        source_model: str,
        source_round: int,
    ) -> Fact:
        """Record a new fact. First writer wins - never overwrites an existing slot.

        Caller MUST hold lock_for(prompt_index) when calling this. The entry
        is auto-created if missing, so callers that go through simulate_answer
        directly (e.g. tests without explicit lock acquisition) still work.
        """
        pf = self.ensure_sync(prompt_index)
        existing = pf.facts.get(slot)
        if existing is not None:
            return existing
        fact = Fact(
            slot=slot,
            answer=answer,
            source_question=source_question,
            source_model=source_model,
            source_round=source_round,
        )
        pf.facts[slot] = fact
        self._commit_log.append({
            "prompt_index": prompt_index,
            "slot": slot,
            "answer": answer,
            "source_question": source_question,
            "source_model": source_model,
            "source_round": source_round,
        })
        return fact

    def seed(self, prompt_index: int, facts: list[dict]) -> int:
        """Pre-populate the per-prompt fact store with user-approved gold
        facts before any model runs. Used for ATaR-sourced (and manual) prompts
        where the trader's product attributes are known up front - candidates
        Q&A will then hit a warm store for the most common slots.

        Each fact: {slot: str, answer: str, source_question?: str}. Slots that
        already exist are skipped (first-writer-wins also applies to seeding).
        Returns the number of facts that landed.

        Synchronous - call before the run starts, when no models are racing.
        """
        if not facts:
            return 0
        pf = self.ensure_sync(prompt_index)
        landed = 0
        for f in facts:
            slot = str(f.get("slot", "")).strip()
            answer = str(f.get("answer", "")).strip()
            if not slot or not answer:
                continue
            if slot in pf.facts:
                continue
            source_q = str(f.get("source_question", "(pre-seeded from gold facts)"))
            fact = Fact(
                slot=slot,
                answer=answer,
                source_question=source_q,
                source_model="seed",
                source_round=0,
            )
            pf.facts[slot] = fact
            self._commit_log.append({
                "prompt_index": prompt_index,
                "slot": slot,
                "answer": answer,
                "source_question": source_q,
                "source_model": "seed",
                "source_round": 0,
            })
            landed += 1
        return landed

    def drain_new_commits(self, from_index: int) -> tuple[list[dict], int]:
        """Return commits appended to the log since `from_index`, and the new
        end-of-log index. Used by run_benchmark to stream live SSE events.
        """
        new = self._commit_log[from_index:]
        return new, len(self._commit_log)

    def all_prompts(self) -> dict[int, dict[str, Fact]]:
        """Return full snapshot across all prompts for debug/analytics."""
        return {pi: dict(pf.facts) for pi, pf in self._by_prompt.items()}

    def as_dict(self, prompt_index: int) -> list[dict[str, Any]]:
        """Serialisable snapshot for one prompt (for persisting in run JSON)."""
        return [
            {
                "slot": f.slot,
                "answer": f.answer,
                "source_question": f.source_question,
                "source_model": f.source_model,
                "source_round": f.source_round,
            }
            for f in self.snapshot(prompt_index).values()
        ]
