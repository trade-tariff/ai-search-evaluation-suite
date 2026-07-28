from __future__ import annotations

import json
import re
from collections import Counter

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity as sklearn_cosine

from schemas import CompletionResult, EvaluationResult


def _extract_codes(text: str) -> list[str]:
    """Extract commodity codes from a response (JSON or freeform)."""
    codes: list[str] = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            for answer in parsed.get("answers", []):
                if "commodity_code" in answer:
                    codes.append(str(answer["commodity_code"]))
    except (json.JSONDecodeError, TypeError):
        pass

    if not codes:
        # Fallback: find 10-digit numeric sequences typical of HS codes
        codes = re.findall(r"\b\d{10}\b", text)

    return codes


def _detect_response_type(text: str) -> str:
    """Detect whether the response contains answers, questions, or an error."""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            if "answers" in parsed:
                return "answers"
            if "questions" in parsed:
                return "questions"
            if "error" in parsed:
                return "error"
    except (json.JSONDecodeError, TypeError):
        pass

    lower = text.lower()
    if "commodity_code" in lower:
        return "answers"
    if "question" in lower:
        return "questions"
    return "unknown"


def compute_text_similarity(text_a: str, text_b: str) -> float:
    """TF-IDF cosine similarity between two response texts."""
    if not text_a.strip() or not text_b.strip():
        return 0.0
    vectorizer = TfidfVectorizer()
    try:
        matrix = vectorizer.fit_transform([text_a, text_b])
        sim = sklearn_cosine(matrix[0:1], matrix[1:2])[0][0]
        return float(sim)
    except ValueError:
        return 0.0


def compute_code_match(baseline_codes: list[str], target_codes: list[str]) -> dict:
    """Compare commodity codes between baseline and target responses.

    Production AI search returns a RANKED list of codes (up to 5) with
    confidence labels, sorted by confidence. These metrics reflect that -
    strict top-1 is one signal, but the ranked list gives richer ones.

    Returns a dict of deterministic code-agreement signals:
      top1_match           - exact top-1 match (bool)
      top3_hit             - ref's top-1 in candidate's top-3 (bool)
      top5_overlap         - Jaccard overlap of top-5 (0-1)
      mean_reciprocal_rank - 1/rank of ref's top in candidate list (0 if >top5)
      code_match_score     - position-weighted aggregate (legacy)
      heading_match        - first 4 digits agree (bool)
      chapter_match        - first 2 digits agree (bool)
      hierarchical_score   - 0-1 graded by deepest matching prefix
    """
    if not baseline_codes or not target_codes:
        return {
            "top1_match": False,
            "top3_hit": False,
            "top5_overlap": 0.0,
            "mean_reciprocal_rank": 0.0,
            "code_match_score": 0.0,
            "heading_match": False,
            "chapter_match": False,
            "hierarchical_score": 0.0,
        }

    top1_match = target_codes[0] == baseline_codes[0]

    baseline_top5 = set(baseline_codes[:5])
    target_top5 = set(target_codes[:5])
    overlap = (
        len(baseline_top5 & target_top5) / len(baseline_top5) if baseline_top5 else 0.0
    )

    # Ranking-aware metrics: where does the reference's top-1 appear in the
    # candidate's ranked list?
    ref_top = baseline_codes[0]
    target_top3 = target_codes[:3]
    top3_hit = ref_top in target_top3
    mrr = 0.0
    for i, code in enumerate(target_codes[:5]):
        if code == ref_top:
            mrr = 1.0 / (i + 1)
            break

    # Weighted score: top1 worth more, descending importance
    score = 0.0
    weights = [0.4, 0.25, 0.15, 0.12, 0.08]
    for i, w in enumerate(weights):
        if i < len(baseline_codes) and i < len(target_codes):
            if baseline_codes[i] == target_codes[i]:
                score += w
            elif baseline_codes[i] in target_codes[:5]:
                score += w * 0.5

    # Hierarchical match on the top-1 codes. HS codes are nested by prefix:
    # 2 digits = chapter, 4 = heading, 6 = subheading, 8 = commodity,
    # 10 = CN/commodity-plus. Give graded credit for partial matches.
    def _common_prefix_len(a: str, b: str) -> int:
        n = 0
        for ca, cb in zip(a, b):
            if ca != cb:
                break
            n += 1
        return n

    prefix = _common_prefix_len(baseline_codes[0], target_codes[0])
    chapter_match = prefix >= 2
    heading_match = prefix >= 4
    # 0-1 graded: 10-digit full match = 1.0, 8 = 0.8, 6 = 0.6, 4 = 0.4, 2 = 0.2, else 0
    hierarchical_score = min(prefix, 10) / 10.0

    return {
        "top1_match": top1_match,
        "top3_hit": top3_hit,
        "top5_overlap": overlap,
        "mean_reciprocal_rank": mrr,
        "code_match_score": score,
        "heading_match": heading_match,
        "chapter_match": chapter_match,
        "hierarchical_score": hierarchical_score,
    }


def compute_gold_metrics(
    candidate_codes: list[str],
    gold_code: str | None,
) -> dict:
    """Score a candidate's top-1 code against a ground-truth gold code.

    Independent of the model reference: this is pure "did the model land on
    the known-correct code". Returned dict has None values when gold_code is
    missing so callers can distinguish "didn't match" from "not evaluated".

    Returns:
      gold_top1_match          - candidate top-1 exactly matches gold
      gold_heading_match       - first 4 digits agree
      gold_chapter_match       - first 2 digits agree
      gold_hierarchical_score  - deepest common prefix / 10 (0-1)
    """
    if not gold_code or not candidate_codes:
        return {
            "gold_top1_match": None,
            "gold_heading_match": None,
            "gold_chapter_match": None,
            "gold_hierarchical_score": None,
        }

    cand_top = str(candidate_codes[0])
    gold = str(gold_code)

    prefix = 0
    for a, b in zip(cand_top, gold):
        if a != b:
            break
        prefix += 1

    return {
        "gold_top1_match": cand_top == gold,
        "gold_heading_match": prefix >= 4,
        "gold_chapter_match": prefix >= 2,
        "gold_hierarchical_score": round(min(prefix, 10) / 10.0, 4),
    }


def _schema_valid_score(text: str) -> float:
    """Binary 0/1 - is the response valid JSON with answers/questions at top level?"""
    if not text:
        return 0.0
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return 0.0
    if not isinstance(parsed, dict):
        return 0.0
    # Accept either answers[] (with commodity_code) or questions[] (with
    # question+options). Anything else is partial credit 0.5.
    if "answers" in parsed and isinstance(parsed["answers"], list):
        ok = all(isinstance(a, dict) and "commodity_code" in a for a in parsed["answers"])
        return 1.0 if ok else 0.5
    if "questions" in parsed and isinstance(parsed["questions"], list):
        ok = all(isinstance(q, dict) and "question" in q and "options" in q for q in parsed["questions"])
        return 1.0 if ok else 0.5
    return 0.3


def evaluate_pair(
    baseline: CompletionResult,
    target: CompletionResult,
    gold_code: str | None = None,
) -> EvaluationResult:
    """Evaluate a target model's response against the baseline for one prompt.

    All dimensions returned here are DETERMINISTIC - computed locally from
    the run data without LLM calls. The LLM judge adds only question_quality
    and fact_consistency on top.

    If gold_code is provided, also scores the target against the ground-truth
    code (independent of the baseline). These are exposed as the gold_* fields
    on the returned EvaluationResult.
    """
    # Text similarity
    cos_sim = compute_text_similarity(baseline.response_text, target.response_text)

    # Structured code comparison (deterministic accuracy signals)
    baseline_codes = _extract_codes(baseline.response_text)
    target_codes = _extract_codes(target.response_text)
    code_metrics = compute_code_match(baseline_codes, target_codes)
    gold_metrics = compute_gold_metrics(target_codes, gold_code)

    # Delta score: weighted combination mapped to [-1, +1]
    raw_score = cos_sim * 0.7 + code_metrics["code_match_score"] * 0.3
    delta_score = raw_score * 2 - 1

    # Speed factor using total latency across all Q&A rounds
    t_lat = target.total_latency_ms or target.latency_ms
    b_lat = baseline.total_latency_ms or baseline.latency_ms
    speed_factor = b_lat / t_lat if t_lat > 0 else 0.0

    t_cost = target.total_cost or target.cost
    b_cost = baseline.total_cost or baseline.cost

    # JSON schema compliance - deterministic replacement for judge_structured_output
    schema_valid = _schema_valid_score(target.response_text)

    # Q&A efficiency: across all questions the target asked for this prompt,
    # how many introduced a NEW slot vs recalled an existing one? A model
    # that mostly asks already-set questions is being redundant.
    total_questions = 0
    new_slots_set = 0
    for rnd in target.rounds:
        trace = rnd.simulator_trace or []
        questions = rnd.questions_asked or []
        total_questions += len(questions)
        # trace[i].consistent_with_prior=False => new slot committed this question
        for t in trace:
            if isinstance(t, dict) and not t.get("consistent_with_prior", False):
                new_slots_set += 1
    question_efficiency = (
        new_slots_set / total_questions if total_questions > 0 else 1.0
    )

    # Rounds efficiency: fewer rounds = more decisive.
    # Mirrors MAX_ROUNDS in benchmark.py (7, matching production's
    # interactive_search_max_questions); 1 round = 1.0, 7 rounds = 0.0.
    MAX_ROUNDS_FLOOR = 7
    rounds_efficiency = 1.0 - max(0, min(target.total_rounds, MAX_ROUNDS_FLOOR) - 1) / max(1, MAX_ROUNDS_FLOOR - 1)

    return EvaluationResult(
        model_id=target.model_id,
        prompt_index=target.prompt_index,
        cosine_similarity=round(cos_sim, 4),
        code_match_score=round(code_metrics["code_match_score"], 4),
        top1_match=code_metrics["top1_match"],
        top3_hit=code_metrics["top3_hit"],
        top5_overlap=round(code_metrics["top5_overlap"], 4),
        mean_reciprocal_rank=round(code_metrics["mean_reciprocal_rank"], 4),
        heading_match=code_metrics["heading_match"],
        chapter_match=code_metrics["chapter_match"],
        hierarchical_score=round(code_metrics["hierarchical_score"], 4),
        schema_valid=round(schema_valid, 2),
        total_questions=total_questions,
        new_slots_set=new_slots_set,
        question_efficiency=round(question_efficiency, 4),
        rounds_efficiency=round(rounds_efficiency, 4),
        gold_code=str(gold_code) if gold_code else None,
        gold_top1_match=gold_metrics["gold_top1_match"],
        gold_heading_match=gold_metrics["gold_heading_match"],
        gold_chapter_match=gold_metrics["gold_chapter_match"],
        gold_hierarchical_score=gold_metrics["gold_hierarchical_score"],
        delta_score=round(delta_score, 4),
        total_latency_ms=round(t_lat, 1),
        baseline_total_latency_ms=round(b_lat, 1),
        speed_factor=round(speed_factor, 3),
        total_cost=round(t_cost, 6),
        baseline_total_cost=round(b_cost, 6),
        total_rounds=target.total_rounds,
        baseline_total_rounds=baseline.total_rounds,
    )


def detect_response_type(result: CompletionResult) -> str:
    if result.error:
        return "error"
    return _detect_response_type(result.response_text)


def compute_consensus(
    panel_results: list[CompletionResult],
    prompt_index: int,
) -> tuple[CompletionResult, float]:
    """Compute consensus from multiple panel model results for one prompt.

    Returns (consensus_result, panel_agreement) where:
    - consensus_result is a synthetic CompletionResult with model_id="consensus"
    - panel_agreement is the avg pairwise cosine similarity (0-1) across panel members
    """
    valid = [r for r in panel_results if r.prompt_index == prompt_index and not r.error]

    if not valid:
        return CompletionResult(
            model_id="consensus",
            prompt_index=prompt_index,
            response_text="",
            response_type="error",
            error="No valid panel results",
        ), 0.0

    if len(valid) == 1:
        r = valid[0]
        return CompletionResult(
            model_id="consensus",
            prompt_index=prompt_index,
            response_text=r.response_text,
            response_type=r.response_type,
            total_rounds=r.total_rounds,
            total_latency_ms=r.total_latency_ms,
            total_input_tokens=r.total_input_tokens,
            total_output_tokens=r.total_output_tokens,
            total_cost=r.total_cost,
            input_tokens=r.total_input_tokens,
            output_tokens=r.total_output_tokens,
            latency_ms=r.total_latency_ms,
            cost=r.total_cost,
        ), 1.0

    # Compute pairwise similarities to find centroid response
    texts = [r.response_text for r in valid]
    non_empty = [t for t in texts if t.strip()]
    if len(non_empty) < 2:
        # Fall back to first valid result
        r = valid[0]
        return CompletionResult(
            model_id="consensus",
            prompt_index=prompt_index,
            response_text=r.response_text,
            response_type=r.response_type,
            total_rounds=r.total_rounds,
            total_latency_ms=r.total_latency_ms,
            total_input_tokens=r.total_input_tokens,
            total_output_tokens=r.total_output_tokens,
            total_cost=r.total_cost,
            input_tokens=r.total_input_tokens,
            output_tokens=r.total_output_tokens,
            latency_ms=r.total_latency_ms,
            cost=r.total_cost,
        ), 0.0

    try:
        vectorizer = TfidfVectorizer()
        matrix = vectorizer.fit_transform(texts)
        sim_matrix = sklearn_cosine(matrix)
    except ValueError:
        r = valid[0]
        return CompletionResult(
            model_id="consensus",
            prompt_index=prompt_index,
            response_text=r.response_text,
            response_type=r.response_type,
            total_rounds=r.total_rounds,
            total_latency_ms=r.total_latency_ms,
            total_input_tokens=r.total_input_tokens,
            total_output_tokens=r.total_output_tokens,
            total_cost=r.total_cost,
            input_tokens=r.total_input_tokens,
            output_tokens=r.total_output_tokens,
            latency_ms=r.total_latency_ms,
            cost=r.total_cost,
        ), 0.0

    n = len(valid)

    # Panel agreement = average of all pairwise similarities (excluding self)
    pair_sims = []
    for i in range(n):
        for j in range(i + 1, n):
            pair_sims.append(float(sim_matrix[i][j]))
    panel_agreement = sum(pair_sims) / len(pair_sims) if pair_sims else 0.0

    # Centroid = panel member with highest average similarity to all others
    avg_sims = []
    for i in range(n):
        others = [float(sim_matrix[i][j]) for j in range(n) if j != i]
        avg_sims.append(sum(others) / len(others) if others else 0.0)
    centroid_idx = int(np.argmax(avg_sims))
    centroid = valid[centroid_idx]

    # Average latency/cost across panel for fair speed_factor comparison
    avg_latency = sum(r.total_latency_ms for r in valid) / n
    avg_cost = sum(r.total_cost for r in valid) / n
    avg_rounds = sum(r.total_rounds for r in valid) / n

    return CompletionResult(
        model_id="consensus",
        prompt_index=prompt_index,
        response_text=centroid.response_text,
        response_type=centroid.response_type,
        total_rounds=round(avg_rounds),
        total_latency_ms=round(avg_latency, 1),
        total_input_tokens=sum(r.total_input_tokens for r in valid),
        total_output_tokens=sum(r.total_output_tokens for r in valid),
        total_cost=round(avg_cost, 6),
        input_tokens=sum(r.total_input_tokens for r in valid),
        output_tokens=sum(r.total_output_tokens for r in valid),
        latency_ms=round(avg_latency, 1),
        cost=round(avg_cost, 6),
    ), round(panel_agreement, 4)
