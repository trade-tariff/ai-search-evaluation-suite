"""Trader simulator with per-prompt fact store.

Why this exists
---------------
Candidate models ask different wording for the same concept ("what form?" vs
"what shape?" vs "in what state?"). Exact-text caching never hits, and the old
abstain->options[0] fallback made final classifications sensitive to each
model's option ordering - the opposite of the apples-to-apples property we
wanted.

This rewrite has one goal: make the simulator's answers SEMANTICALLY consistent
across every model running the same prompt. The simulator is given the
per-prompt fact store alongside the current question, and must:
  1. Coin a short snake_case slot label for the question
  2. If that slot is already in the fact store, pick the option that matches
     the stored answer (never contradicts prior commitments)
  3. Otherwise, pick the most plausible option for a trader with the raw query
  4. NEVER abstain - always commit to one option from the list

One LLM call per question (gpt-5.4 low by default). The slot vocabulary grows
organically per prompt; there is no predefined slot list.

Concurrency
-----------
Callers must hold `fact_store.lock_for(prompt_index)` around each call, so
sibling and concurrent model calls serialise per prompt. This ensures the
first model to commit a slot is visible to every subsequent caller for that
prompt.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from openai import AsyncOpenAI

from _retry import with_retry_and_limit
from fact_store import FactStore
from schemas import SimulatorConfig

SYSTEM_PROMPT = """\
You are simulating a real trader classifying goods under the UK tariff. A
classifier is asking clarifying questions about your goods. Your only knowledge
of the goods is the raw query you originally typed in, plus any facts you have
ALREADY committed to about this same product in previous questions.

Rules:
1. If any prior fact in the "Known facts" list is the answer to this question
   (or a rewording of it), pick the option that matches that fact. Never
   contradict a prior commitment.
2. If the question asks something new that the query doesn't answer, pick the
   most plausible option for a trader with that query. Prefer common
   generic interpretations over exotic ones. Commit to the choice.
3. NEVER refuse or abstain. Always pick exactly one option from the list.
4. Assign the question a short snake_case slot label describing what it asks
   about (e.g. "form", "grade", "intended_use", "packing_size", "grain_type").
5. If your slot label matches an existing slot in the Known facts list,
   reuse that exact label and set consistent_with_prior=true. Otherwise coin
   a new label and set consistent_with_prior=false.

Respond ONLY with valid JSON in this exact format:
{
  "slot": "<snake_case label>",
  "chosen": "<exact option text>",
  "consistent_with_prior": <true|false>,
  "reasoning": "<one short sentence>"
}"""

# When an Oracle (authoritative product description, e.g. an ATaR ruling body)
# is supplied, the simulator switches from "guess plausibly" to "extract from
# the Oracle". Prior facts still take precedence (first-writer-wins for
# consistency), then Oracle, then fallback to plausible-from-query.
SYSTEM_PROMPT_WITH_ORACLE = """\
You are simulating a real trader classifying goods under the UK tariff. A
classifier is asking clarifying questions about your goods. You have THREE
sources of truth, in priority order:

  (1) Known facts you've already committed about this product (highest priority -
      never contradict)
  (2) The Oracle: an authoritative description of the actual goods (use this
      to answer any new question)
  (3) The raw search query you originally typed in (lowest priority; use only
      if the Oracle is silent)

The Oracle is the ground truth about what this product really is. The raw
query is just the search term you used; do not let it override the Oracle
when they conflict.

Rules:
1. If any prior fact in "Known facts" answers this question (or a rewording of
   it), pick the option that matches. Never contradict a prior commitment.
2. Otherwise read the Oracle and pick the option most consistent with it.
   Quote-or-paraphrase the relevant Oracle phrase in your reasoning so it's
   auditable.
3. If the Oracle is genuinely silent on the topic, fall back to the most
   plausible interpretation given the raw query.
4. NEVER refuse or abstain. Always pick exactly one option from the list.
5. Assign the question a short snake_case slot label (e.g. "form", "grade",
   "intended_use"). Reuse an existing slot from Known facts when it fits and
   set consistent_with_prior=true; otherwise coin a new label.

Respond ONLY with valid JSON in this exact format:
{
  "slot": "<snake_case label>",
  "chosen": "<exact option text>",
  "consistent_with_prior": <true|false>,
  "reasoning": "<one short sentence; if Oracle was used, hint at which phrase>"
}"""


_TRAILING_PUNCT = ".,;:!?\"'`"


def _tidy(s: str) -> str:
    return s.strip().strip(_TRAILING_PUNCT).strip().lower()


def _match_to_options(choice: str, options: list[str]) -> str | None:
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


def _format_facts(facts: dict) -> str:
    """Render the per-prompt facts for the system prompt."""
    if not facts:
        return "(no prior commitments - this is the first question)"
    lines = []
    for slot, f in facts.items():
        lines.append(f'- slot="{slot}" | answer="{f.answer}" | asked as: "{f.source_question}"')
    return "\n".join(lines)


async def simulate_answer(
    client: AsyncOpenAI,
    fact_store: FactStore,
    prompt_index: int,
    round_number: int,
    model_id: str,
    query: str,
    question: str,
    options: list[str],
    config: SimulatorConfig,
    oracle_text: str | None = None,
    event_bus: "asyncio.Queue | None" = None,
) -> dict[str, Any]:
    """Pick an answer for one question, consistent with per-prompt facts.

    The caller MUST hold `await fact_store.lock_for(prompt_index)` around this
    call. Sibling/cross-model calls serialise per prompt so writes are visible.

    When `oracle_text` is provided (e.g. the body of an ATaR ruling), the
    simulator treats it as the authoritative description of the actual goods
    and uses it to answer questions the seeded facts don't already cover. The
    raw query becomes a low-priority hint. This is the path that makes
    ground-truth-backed benchmarks reliable: the same product description
    feeds every model's Q&A loop, so candidate disagreement reflects the
    model, not simulator variance.

    Returns a dict with trace fields:
      chosen (str)               - exact option text picked
      slot (str)                 - snake_case slot label
      reasoning (str)            - one-sentence rationale from the LLM
      consistent_with_prior (bool) - True if reused an existing slot
      from_store (bool)          - True if the answer was served from the store
                                   without needing an LLM recall
      cost (float)               - USD cost (0 if from_store)
      latency_ms (float)         - wallclock ms (0 if from_store)
    """
    if not options:
        return {
            "chosen": "Yes",
            "slot": "unanswerable",
            "reasoning": "No options provided; defaulted to Yes.",
            "consistent_with_prior": False,
            "from_store": False,
            "cost": 0.0,
            "latency_ms": 0.0,
        }

    facts = fact_store.snapshot(prompt_index)

    # Build the prompt. Show known facts + current question + options. When
    # an oracle is present, prepend it as the authoritative product source.
    options_block = "\n".join(f"- {opt}" for opt in options)
    facts_block = _format_facts(facts)
    use_oracle = bool(oracle_text and oracle_text.strip())
    if use_oracle:
        # Truncate to keep the simulator prompt cost bounded; ATaR ruling
        # bodies are usually 200-2000 chars but some run longer.
        oracle_snippet = oracle_text.strip()
        if len(oracle_snippet) > 6000:
            oracle_snippet = oracle_snippet[:6000] + "\n[... truncated ...]"
        user_prompt = (
            f"## Oracle (authoritative product description)\n{oracle_snippet}\n\n"
            f"## Raw trader query (low-priority hint)\n{query}\n\n"
            f"## Known facts for this product\n{facts_block}\n\n"
            f"## Current question\n{question}\n\n"
            f"## Options\n{options_block}"
        )
        system_prompt = SYSTEM_PROMPT_WITH_ORACLE
    else:
        user_prompt = (
            f"## Raw trader query\n{query}\n\n"
            f"## Known facts for this product\n{facts_block}\n\n"
            f"## Current question\n{question}\n\n"
            f"## Options\n{options_block}"
        )
        system_prompt = SYSTEM_PROMPT

    kwargs: dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    model_name = config.model.lower()
    uses_reasoning_defaults = model_name.startswith(("gpt-5", "o"))
    if config.reasoning_effort:
        kwargs["reasoning_effort"] = config.reasoning_effort
    elif not uses_reasoning_defaults:
        kwargs["temperature"] = config.temperature

    start = time.perf_counter()
    try:
        resp = await with_retry_and_limit(
            "openai",
            lambda: client.chat.completions.create(**kwargs),
        )
    except Exception as exc:
        # Hard failure path (after retries exhausted or non-transient error) -
        # pick options[0], coin a fallback slot so subsequent models asking
        # the same verbatim question still see consistency. Do NOT record in
        # the fact store since we have no confident slot name here.
        return {
            "chosen": options[0],
            "slot": "_error_fallback",
            "reasoning": f"Simulator API error after retries, picked first option: {str(exc)[:160]}",
            "consistent_with_prior": False,
            "from_store": False,
            "cost": 0.0,
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
        }
    latency_ms = (time.perf_counter() - start) * 1000

    usage = resp.usage or type("U", (), {"prompt_tokens": 0, "completion_tokens": 0})()
    inp = getattr(usage, "prompt_tokens", 0) or 0
    out = getattr(usage, "completion_tokens", 0) or 0
    cost = (
        inp * config.input_cost_per_million / 1_000_000
        + out * config.output_cost_per_million / 1_000_000
    )

    raw_text = (resp.choices[0].message.content or "").strip()
    if raw_text.startswith("```"):
        lines = [l for l in raw_text.split("\n") if not l.strip().startswith("```")]
        raw_text = "\n".join(lines)

    slot: str = ""
    chosen_raw: str = ""
    reasoning: str = ""
    consistent: bool = False
    try:
        data = json.loads(raw_text)
        slot = str(data.get("slot", "")).strip() or "unlabelled"
        chosen_raw = str(data.get("chosen", "")).strip()
        consistent = bool(data.get("consistent_with_prior", False))
        reasoning = str(data.get("reasoning", ""))[:400]
    except (json.JSONDecodeError, TypeError):
        slot = "_parse_error"
        reasoning = f"Simulator returned unparseable JSON: {raw_text[:160]}"

    # Normalise the LLM's chosen string back to one of the exact option strings.
    chosen = _match_to_options(chosen_raw, options) or options[0]

    # Record (or reconcile) with the fact store. First writer wins, so if the
    # LLM claimed consistent_with_prior and the slot already exists, the stored
    # answer is authoritative - remap to an option that matches it.
    existing = facts.get(slot)
    if existing is not None:
        remapped = _match_to_options(existing.answer, options)
        if remapped is not None:
            chosen = remapped
        consistent = True
    else:
        # New slot - commit.
        fact_store.record(
            prompt_index=prompt_index,
            slot=slot,
            answer=chosen,
            source_question=question,
            source_model=model_id,
            source_round=round_number,
        )
        # Live-push the commit so the orchestrator's phase loop can yield it
        # as an SSE event mid-task. Mirror fact_store._commit_log shape so
        # the existing frontend handler keys off the same fields. Use
        # put_nowait so a saturated bus never blocks the simulator.
        if event_bus is not None:
            try:
                event_bus.put_nowait((
                    "live",
                    "simulator:commit",
                    {
                        "prompt_index": prompt_index,
                        "slot": slot,
                        "answer": chosen,
                        "source_question": question,
                        "source_model": model_id,
                        "source_round": round_number,
                    },
                ))
            except asyncio.QueueFull:
                pass

    return {
        "chosen": chosen,
        "slot": slot,
        "reasoning": reasoning,
        "consistent_with_prior": consistent,
        "from_store": False,
        "cost": round(cost, 6),
        "latency_ms": round(latency_ms, 1),
    }
