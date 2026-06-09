"""LLM-as-Judge evaluation.

Scores a target model's response against the baseline on:
- classification_accuracy (0-10): correctness of HS codes on their own merits
- question_quality (0-10): appropriateness of strategy (direct answer vs questions)
- structured_output (0-10): JSON compliance, correct schema
- overall (0-10): weighted quality score (~60% accuracy, ~30% strategy, ~10% structure)
"""

from __future__ import annotations

import json
import time

from openai import AsyncOpenAI

from _retry import with_retry_and_limit
from schemas import JudgeConfig

# The LLM judge scores TWO dimensions: fact_consistency and question_quality.
# Everything else (accuracy, schema validity, efficiency, speed, cost) has a
# deterministic equivalent computed directly from the run data - cheaper,
# reproducible, and unbiased by judge self-preference. The two LLM dimensions
# below are the only ones where semantic understanding genuinely helps.
_SCORING_RUBRICS = """\
**fact_consistency** (0-10):
Does the target's final commodity code respect every committed fact in the
per-prompt fact store shown below? Examples of inconsistency:
 - Fact "parboiled = No" committed but the final code is for parboiled rice
 - Fact "form = powder" committed but the final code is for pellets
 - Fact "grade = stainless steel" committed but the final code is for
   non-alloy steel

- 9-10: Every committed fact is consistent with the final code
- 7-8: One minor fact loosely contradicted; code still defensible
- 4-6: A fact materially contradicted (e.g. material/grade mismatch)
- 0-3: Multiple facts contradicted, or code clearly in a different product area

If the fact store is empty (direct-answer prompt, no questions asked) return 10.
If no final code was produced, return 0.

**question_quality** (0-10):
Evaluate the clarifying questions the target asked (see the target response).
A deterministic signal (question_efficiency = new_slots_set / total_questions)
already measures redundancy; this dimension captures the NUANCE deterministic
metrics can't: phrasing clarity, whether the question would discriminate
meaningful HS codes, and whether the options cover the real decision space.

- 9-10: Few, sharp, well-phrased questions that each discriminate distinct HS subheadings
- 7-8: Reasonable questions, mostly productive, decent option coverage
- 5-6: Some questions vague, redundant with retrieval, or have poor option coverage
- 3-4: Wasteful, off-target, or ambiguously worded questions
- 0-2: Nonsensical questions or questions that don't help classify

For direct-answer responses (0 questions): return 10 if the query was
genuinely unambiguous (a specific product name / CAS number), 5-6 if there
was ambiguity questions would have helped with, 0-2 if the target guessed
through clear ambiguity."""

_JSON_FORMAT = """\
Respond ONLY with valid JSON in this exact format:
{
  "fact_consistency": <0-10>,
  "question_quality": <0-10>,
  "reasoning": "<1-2 sentence explanation covering both scores>"
}"""

# Judge prompt - scores fact_consistency + question_quality. Everything else
# (accuracy, schema validity, efficiency, etc.) is computed deterministically
# in benchmark code. The two LLM dimensions here are the ones where semantic
# understanding genuinely helps beyond deterministic metrics.
DEFAULT_SYSTEM_PROMPT = f"""\
You are an expert evaluator for UK goods classification (HS/commodity code assignment).

You will be given:
1. The original goods description query
2. A REFERENCE response (the pinned gold standard for this run)
3. A TARGET response (the candidate being evaluated)
4. The per-prompt fact store: a list of slot/value pairs the simulator
   committed to while resolving the Q&A. Every candidate in this run classified
   the same hypothetical product, defined by these facts.

Score the TARGET on two dimensions:

{_SCORING_RUBRICS}

{_JSON_FORMAT}"""

# Standalone variant, kept for API compatibility with is_baseline=True callers.
# Not currently used (the reference is never judged; it is the answer key).
DEFAULT_BASELINE_SYSTEM_PROMPT = f"""\
You are an expert evaluator for UK goods classification (HS/commodity code assignment).

You will be given:
1. The original goods description query
2. A single response to evaluate
3. The per-prompt fact store for context

{_SCORING_RUBRICS}

{_JSON_FORMAT}"""


def _format_facts_block(facts: list[dict] | None) -> str:
    if not facts:
        return "(empty - this prompt had no clarifying Q&A, or was resolved directly)"
    lines = []
    for f in facts:
        slot = f.get("slot", "?")
        ans = f.get("answer", "?")
        src = f.get("source_model", "?")
        rnd = f.get("source_round", "?")
        lines.append(f'- {slot} = "{ans}" (committed by {src} in round {rnd})')
    return "\n".join(lines)


def _build_judge_prompt(
    query: str,
    baseline_text: str,
    target_text: str,
    max_len: int,
    baseline_rounds: int = 1,
    target_rounds: int = 1,
    is_baseline: bool = False,
    facts: list[dict] | None = None,
) -> str:
    facts_block = _format_facts_block(facts)
    if is_baseline:
        return f"""## Original Query
{query}

## Per-prompt fact store
{facts_block}

## Response (completed in {target_rounds} round{"s" if target_rounds != 1 else ""})
{target_text[:max_len]}"""

    return f"""## Original Query
{query}

## Per-prompt fact store
{facts_block}

## Reference Response (completed in {baseline_rounds} round{"s" if baseline_rounds != 1 else ""})
{baseline_text[:max_len]}

## Target Response (completed in {target_rounds} round{"s" if target_rounds != 1 else ""})
{target_text[:max_len]}"""


def _parse_judge_response(text: str) -> dict:
    """Parse the judge's JSON response, handling common issues."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    def clamp(v) -> float:
        return max(0.0, min(10.0, float(v)))

    try:
        data = json.loads(text)
        fc_raw = data.get("fact_consistency")
        qq_raw = data.get("question_quality")
        return {
            "fact_consistency": clamp(fc_raw) if fc_raw is not None else None,
            "question_quality": clamp(qq_raw) if qq_raw is not None else None,
            "reasoning": str(data.get("reasoning", ""))[:500],
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        return {
            "fact_consistency": None,
            "question_quality": None,
            "reasoning": f"Judge response parse error: {text[:200]}",
        }


async def judge_response(
    client: AsyncOpenAI,
    query: str,
    baseline_text: str,
    target_text: str,
    judge_config: JudgeConfig | None = None,
    baseline_rounds: int = 1,
    target_rounds: int = 1,
    is_baseline: bool = False,
    facts: list[dict] | None = None,
) -> dict:
    """Call the judge model to score a response.

    When is_baseline=True, uses a standalone prompt that evaluates the response
    on absolute quality without a comparison baseline.

    Returns dict with scores (0-10) and cost info.
    """
    cfg = judge_config or JudgeConfig()
    if is_baseline:
        # Standalone evaluation - use baseline-specific prompt
        system_prompt = DEFAULT_BASELINE_SYSTEM_PROMPT
    else:
        system_prompt = cfg.system_prompt if cfg.system_prompt.strip() else DEFAULT_SYSTEM_PROMPT
    user_prompt = _build_judge_prompt(
        query, baseline_text, target_text, cfg.max_response_length,
        baseline_rounds=baseline_rounds, target_rounds=target_rounds,
        is_baseline=is_baseline, facts=facts,
    )

    start = time.perf_counter()
    try:
        kwargs: dict = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "timeout": 60.0,
        }
        if cfg.reasoning_effort:
            # Reasoning models (o-series, gpt-5.x) don't support temperature
            kwargs["reasoning_effort"] = cfg.reasoning_effort
        else:
            kwargs["temperature"] = 0.0
        # Dedicated "openai_judge" pool so judge calls don't compete with
        # model/simulator calls for the same semaphore - when model calls
        # hold all the slots (xhigh reasoning blocks 1-2 min each), judges
        # would otherwise starve and all error out.
        resp = await with_retry_and_limit(
            "openai_judge",
            lambda: client.chat.completions.create(**kwargs),
        )
    except Exception as exc:
        # API error: return None scores so they are excluded from averages
        # rather than treated as zeros (which silently drags the summary down).
        return {
            "fact_consistency": None,
            "question_quality": None,
            "reasoning": f"Judge API error: {str(exc)[:200]}",
            "cost": 0.0,
            "latency_ms": 0.0,
            "error": True,
        }

    latency = (time.perf_counter() - start) * 1000
    usage = resp.usage or type("U", (), {"prompt_tokens": 0, "completion_tokens": 0})()
    inp = getattr(usage, "prompt_tokens", 0) or 0
    out = getattr(usage, "completion_tokens", 0) or 0
    cost = inp * cfg.input_cost_per_million / 1_000_000 + out * cfg.output_cost_per_million / 1_000_000

    text = resp.choices[0].message.content or ""
    scores = _parse_judge_response(text)
    scores["cost"] = round(cost, 6)
    scores["latency_ms"] = round(latency, 1)
    return scores
