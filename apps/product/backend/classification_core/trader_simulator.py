"""Oracle-backed trader answers and in-memory facts, without local DB imports."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from openai import AsyncOpenAI

SIMULATOR_MODEL = os.environ.get("QA_SIMULATOR_MODEL", "gpt-5-mini")
SIMULATOR_MAX_RETRIES = 2


@dataclass
class CommittedFact:
    slot: str
    answer: str
    source_question: str
    round_number: int


@dataclass
class SessionFacts:
    """In-memory facts keyed by the simulator's snake_case slot labels.

    Each session owns its fact mapping. This store has no database dependency;
    callers choose whether to persist its facts.
    """
    facts: dict[str, CommittedFact] = field(default_factory=dict)

    def snapshot(self) -> dict[str, CommittedFact]:
        return dict(self.facts)

    def get(self, slot: str) -> Optional[CommittedFact]:
        return self.facts.get(slot)

    def record(self, slot: str, answer: str, source_question: str, round_number: int) -> None:
        self.facts[slot] = CommittedFact(
            slot=slot, answer=answer, source_question=source_question, round_number=round_number,
        )

    def as_qa_history(self) -> list[dict]:
        """The shape classify_step expects: [{question, answer}, ...]."""
        return [
            {"question": f.source_question, "answer": f.answer}
            for f in self.facts.values()
        ]


_TRAILING_PUNCT = ".,;:!?\"'"


def _tidy(s: str) -> str:
    return s.strip().strip(_TRAILING_PUNCT).strip().lower()


def _match_to_options(choice: str, options: list[str]) -> Optional[str]:
    """Map a free-form chosen string back to one of the exact option strings."""
    if not choice:
        return None
    for opt in options:
        if opt == choice:
            return opt
    tc = _tidy(choice)
    for opt in options:
        if _tidy(opt) == tc:
            return opt
    for opt in options:
        to = _tidy(opt)
        if to and (to in tc or tc in to):
            return opt
    return None


def _format_facts_block(facts: dict[str, CommittedFact]) -> str:
    if not facts:
        return "(no prior commitments - this is the first question)"
    lines = []
    for slot, f in facts.items():
        lines.append(f'- slot="{slot}" answer="{f.answer}" asked-as: "{f.source_question}"')
    return "\n".join(lines)


_SIMULATOR_SYSTEM = """You are simulating a real trader classifying goods under the UK tariff.
A classifier is asking you a clarifying question about your goods. You have:
- An authoritative product description (if provided) - treat as ground truth
- The raw query the trader originally typed (low-priority hint)
- Facts you have ALREADY committed to in previous questions (do not contradict)

The options are numbered 0..N-1. You MUST pick by index from the EXACT list.
Do NOT invent options. Do NOT paraphrase. Do NOT return option text - return
the integer index only.

Rules:
1. If a known fact is the answer (or a rewording), pick the index whose option
   matches it. Never contradict a prior commitment.
2. If the question is new, pick the most plausible option given the oracle
   description (or the raw query if no oracle). Prefer common interpretations.
3. NEVER abstain. ALWAYS pick exactly one index from the provided list.
4. Assign the question a short snake_case slot label (e.g. "material", "form",
   "intended_use"). If a similar slot already exists in known facts, REUSE it.

Reply in JSON only - choice_index MUST be a valid integer in [0, N-1]:
{"slot": "...", "choice_index": 0, "reasoning": "one short sentence"}
"""


def _build_simulator_prompt(
    raw_query: str,
    question: str,
    options: list[str],
    facts: dict[str, CommittedFact],
    oracle_text: Optional[str],
) -> str:
    options_block = "\n".join(f"[{i}] {opt}" for i, opt in enumerate(options))
    facts_block = _format_facts_block(facts)
    n = len(options)
    use_oracle = bool(oracle_text and oracle_text.strip())
    if use_oracle:
        snippet = oracle_text.strip()
        if len(snippet) > 6000:
            snippet = snippet[:6000] + "\n[... truncated ...]"
        return (
            f"## Oracle (authoritative product description)\n{snippet}\n\n"
            f"## Raw trader query (low-priority hint)\n{raw_query}\n\n"
            f"## Known facts for this product\n{facts_block}\n\n"
            f"## Current question\n{question}\n\n"
            f"## Options (pick ONE by index, valid range: 0..{n - 1})\n{options_block}\n\n"
            f"Reply with JSON only: {{\"slot\": \"...\", \"choice_index\": <int in 0..{n - 1}>, \"reasoning\": \"...\"}}"
        )
    return (
        f"## Raw trader query\n{raw_query}\n\n"
        f"## Known facts for this product\n{facts_block}\n\n"
        f"## Current question\n{question}\n\n"
        f"## Options (pick ONE by index, valid range: 0..{n - 1})\n{options_block}\n\n"
        f"Reply with JSON only: {{\"slot\": \"...\", \"choice_index\": <int in 0..{n - 1}>, \"reasoning\": \"...\"}}"
    )


def _parse_indexed_choice(content: str, n_options: int) -> tuple[Optional[int], Optional[str], Optional[str], Optional[str]]:
    """Returns (choice_index, slot, reasoning, error).

    choice_index is an int in [0, n_options) or None on failure.
    """
    if not content:
        return None, None, None, "empty response"
    try:
        data = json.loads(content)
    except Exception as exc:
        return None, None, None, f"json decode failed: {exc!r}"
    raw_idx = data.get("choice_index")
    if isinstance(raw_idx, bool):  # bool is an int subclass - reject
        return None, data.get("slot"), data.get("reasoning"), "choice_index was a boolean"
    if not isinstance(raw_idx, int):
        # Try numeric string
        if isinstance(raw_idx, str) and raw_idx.strip().lstrip("-").isdigit():
            raw_idx = int(raw_idx.strip())
        else:
            return None, data.get("slot"), data.get("reasoning"), f"choice_index not int: {raw_idx!r}"
    if raw_idx < 0 or raw_idx >= n_options:
        return None, data.get("slot"), data.get("reasoning"), f"choice_index {raw_idx} out of range [0, {n_options})"
    slot = (data.get("slot") or "").strip() or None
    reasoning = (data.get("reasoning") or "").strip() or None
    return raw_idx, slot, reasoning, None


async def simulate_trader_answer(
    client: AsyncOpenAI,
    session: SessionFacts,
    raw_query: str,
    question: str,
    options: list[str],
    round_number: int,
    oracle_text: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Pick an answer for one question, using oracle + prior facts.

    The simulator MUST pick by integer index from the supplied options list -
    no free-form text answers, no paraphrases, no hallucinated options.
    Retries up to SIMULATOR_MAX_RETRIES times on parse/range failure with a
    progressively stricter prompt. After exhaustion, returns a result with
    `simulator_failed=True` and no committed fact - the loop should treat
    this as a no-progress round.

    model: the OpenAI model for THIS call only, explicit per-call rather than a
    shared global (AI-1428) -- several runs can execute concurrently in one
    process, so a run's own simulator_model override must never leak into a
    different run's calls. Falls back to the module-level SIMULATOR_MODEL
    default when omitted, which is what every caller before AI-1428 still
    gets without passing anything new.

    Returns: {
        chosen: str          - the EXACT option text picked, OR None if failed
        choice_index: int    - the index picked, OR None if failed
        slot: str
        reasoning: str
        simulator_failed: bool
        attempts: int
        last_error: str | None
    }
    """
    if not options:
        return {
            "chosen": None, "choice_index": None, "slot": "no_options",
            "reasoning": "no options provided", "simulator_failed": True,
            "attempts": 0, "last_error": "no options",
        }

    n = len(options)
    facts = session.snapshot()

    last_error: Optional[str] = None
    last_slot: Optional[str] = None
    last_reasoning: Optional[str] = None

    for attempt in range(SIMULATOR_MAX_RETRIES + 1):
        user_prompt = _build_simulator_prompt(raw_query, question, options, facts, oracle_text)
        if attempt > 0 and last_error:
            user_prompt += f"\n\nYour previous attempt failed: {last_error}. Reply STRICTLY in the required JSON shape with choice_index as an integer in [0, {n - 1}]."
        kwargs = {
            "model": model or SIMULATOR_MODEL,
            "messages": [
                {"role": "system", "content": _SIMULATOR_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }

        try:
            resp = await client.chat.completions.create(**kwargs)
            content = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            last_error = f"API call failed: {exc!r}"
            continue

        idx, slot, reasoning, err = _parse_indexed_choice(content, n)
        if err is None and idx is not None:
            chosen = options[idx]
            slot = slot or f"round_{round_number}"
            session.record(
                slot=slot, answer=chosen, source_question=question, round_number=round_number,
            )
            return {
                "chosen": chosen, "choice_index": idx, "slot": slot,
                "reasoning": reasoning or "",
                "simulator_failed": False, "attempts": attempt + 1, "last_error": None,
            }
        last_error = err
        last_slot = slot or last_slot
        last_reasoning = reasoning or last_reasoning

    # Exhausted retries - return clearly-marked failure without committing a fact.
    # The caller is responsible for handling this (e.g. stop the loop or treat
    # as no-progress).
    return {
        "chosen": None, "choice_index": None,
        "slot": last_slot or f"failed_round_{round_number}",
        "reasoning": last_reasoning or "",
        "simulator_failed": True,
        "attempts": SIMULATOR_MAX_RETRIES + 1,
        "last_error": last_error,
    }
