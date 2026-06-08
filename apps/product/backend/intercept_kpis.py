"""Per-term complexity KPIs.

All computed over the top-K **declarable** candidates returned by hybrid
retrieval (production filter already applied upstream).

Per-level spread + entropy at every cut of the goods nomenclature hierarchy:

    Level       Cut
    ----------- -----------------------------------------------
    section     Roman numeral (I..XXI), from chapters_sections
    chapter     digits 1-2
    heading     digits 1-4
    subheading  digits 1-6
    eight_digit digits 1-8
    declarable  full 10-digit code

For each level we emit:
    n_<level>           Distinct cuts present in the top-K
    entropy_<level>     Shannon entropy in bits of the RRF-score-weighted
                        distribution at that cut.

Plus aggregate signals:

    inflexion_levels    Count of levels where the candidate set splits
                        (cut count grows over the previous level). This is
                        the UPPER BOUND on the number of yes/no questions
                        needed to disambiguate, walking the tree top-down
                        with one question per branching point.

    questions_max       Sum over levels of log2(cut_count / prev_cut_count).
                        Information-theoretic max questions assuming uniform
                        candidates.

    questions_expected  Total Shannon entropy of the candidate distribution
                        (RRF-weighted) in bits. The actual expected yes/no
                        questions an optimal asker needs, given retrieval
                        priors.

    lca_digits          Longest common prefix across all candidates (0..10).
    unresolved_digits   10 - lca_digits.
    mean_indent_depth   Average tree depth (number_indents).

    score_flatness      RRF score concentration (1 = uniform soup).
    other_leaf_share    Fraction of "Other" catch-all leaves
                        (generation_type='ai' — AI-166 contextualised).

Composite complexity (higher = harder, [0..1]):

    complexity = 0.20 * section_spread_norm
               + 0.20 * chapter_spread_norm
               + 0.30 * questions_expected_norm
               + 0.15 * unresolved_digits / 10
               + 0.10 * score_flatness
               + 0.05 * other_leaf_share

Weights bias toward (a) cross-section + cross-chapter spread (retrieval is
in the wrong domain) and (b) high expected Q&A burden (AI can't converge).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


LEVELS = ["section", "chapter", "heading", "subheading", "eight_digit", "declarable"]

# Default option cap per multi-option question. Overridable per-call via the
# `max_options_per_question` arg of compute(). 4 reflects what the team
# currently treats as the realistic UX ceiling; raise this to model a future
# prompt that allows wider picklists, lower it to stress-test the metric.
DEFAULT_MAX_OPTIONS_PER_QUESTION = 4

DEFAULT_WEIGHTS = {
    "section_spread": 0.15,
    "chapter_spread": 0.15,
    "questions_expected": 0.25,
    "unresolved_digits": 0.10,
    "score_flatness": 0.10,
    "other_leaf_share": 0.05,
    # Vagueness fires when retrieval came back with few hits AND those hits had
    # weak cosines. Catches the "gift set" / "baby pink" failure mode where
    # spread/entropy norms collapse to 0 with n_results=1 — leaving composite
    # misleadingly low for textbook intercept candidates.
    "vagueness": 0.20,
}


@dataclass
class TermKPIs:
    term: str
    k: int
    n_results: int
    # Per-level distinct counts
    n_section: int = 0
    n_chapter: int = 0
    n_heading: int = 0
    n_subheading: int = 0
    n_eight_digit: int = 0
    n_declarable: int = 0
    # Per-level entropy (bits, RRF-weighted)
    entropy_section: float = 0.0
    entropy_chapter: float = 0.0
    entropy_heading: float = 0.0
    entropy_subheading: float = 0.0
    entropy_eight_digit: float = 0.0
    entropy_declarable: float = 0.0
    # Aggregate question metrics
    inflexion_levels: int = 0           # count of tree levels where branching happens
    decision_points: int = 0            # total branching nodes anywhere in the tree
    widest_branch: int = 0              # largest single decision (most options at one node)
    worst_case_questions: int = 0       # Realistic TURN count assuming 4-option cap per question: Σ ceil(log_4(width)) over walked-path forks. A 50-child fork = ceil(log_4(50)) = 3 turns; 12-child = 2; binary = 1. Replaces the old "fork count" reading that capped at 6.
    worst_case_bits: float = 0.0        # Information-theoretic walked-path cost: Σ log2(children). Caps-independent — useful for ranking when the option cap changes.
    questions_max: float = 0.0          # bits — kept internal for legacy, hidden in UI
    questions_expected: float = 0.0     # bits — same
    # Depth
    lca_digits: int = 10
    unresolved_digits: int = 0
    mean_indent_depth: float = 0.0
    # Aux
    score_flatness: float = 0.0
    other_leaf_share: float = 0.0
    # Retrieval-failure signals
    top_cosine: float = 0.0             # Strongest vector-leg cosine across the top-K. 1.0 = perfect match; threshold (default 0.35) = barely passed.
    vagueness: float = 0.0              # Composite penalty: hits_norm * cosine_norm. High = retrieval found little AND it found it weakly (real intercept). Low when n_results << K but top_cosine is strong (narrow well-defined product, NOT intercept).
    # Composite
    composite: float = 0.0
    # Diagnostics
    top_section: str = ""
    top_chapter: str = ""
    top_chapter_share: float = 0.0
    top_score: float = 0.0
    bottom_score: float = 0.0
    section_chain: str = ""   # e.g. "VII (90%) | XV (10%)" — quick visual summary

    def as_row(self) -> dict:
        out = {
            "term": self.term,
            "k": self.k,
            "n_results": self.n_results,
            "composite": round(self.composite, 4),
            "questions_max": round(self.questions_max, 3),
            "questions_expected": round(self.questions_expected, 3),
            "inflexion_levels": self.inflexion_levels,
            "decision_points": self.decision_points,
            "widest_branch": self.widest_branch,
            "worst_case_questions": self.worst_case_questions,
            "worst_case_bits": round(self.worst_case_bits, 2),
            "lca_digits": self.lca_digits,
            "unresolved_digits": self.unresolved_digits,
            "mean_indent_depth": round(self.mean_indent_depth, 2),
            "score_flatness": round(self.score_flatness, 4),
            "other_leaf_share": round(self.other_leaf_share, 4),
            "top_cosine": round(self.top_cosine, 4),
            "vagueness": round(self.vagueness, 4),
            "top_section": self.top_section,
            "top_chapter": self.top_chapter,
            "top_chapter_share": round(self.top_chapter_share, 4),
            "section_chain": self.section_chain,
            "top_score": round(self.top_score, 6),
            "bottom_score": round(self.bottom_score, 6),
        }
        for lvl in LEVELS:
            out[f"n_{lvl}"] = getattr(self, f"n_{lvl}")
            out[f"entropy_{lvl}"] = round(getattr(self, f"entropy_{lvl}"), 3)
        return out


def _longest_common_prefix(codes: list[str]) -> int:
    if not codes:
        return 0
    n = len(codes[0])
    for c in codes[1:]:
        while n > 0 and (len(c) < n or c[:n] != codes[0][:n]):
            n -= 1
        if n == 0:
            break
    return n


def _entropy(weights_by_key: dict[str, float]) -> float:
    """Shannon entropy in bits of a weighted distribution over discrete keys."""
    s = sum(weights_by_key.values())
    if s <= 0:
        return 0.0
    probs = [w / s for w in weights_by_key.values() if w > 0]
    if len(probs) <= 1:
        return 0.0
    return -sum(p * math.log2(p) for p in probs)


def _cut(code: str, level: str, chapter_to_section: dict[str, str]) -> str | None:
    if level == "section":
        return chapter_to_section.get(code[:2])
    if level == "chapter":
        return code[:2]
    if level == "heading":
        return code[:4]
    if level == "subheading":
        return code[:6]
    if level == "eight_digit":
        return code[:8]
    if level == "declarable":
        return code
    raise ValueError(level)


def compute(
    term: str,
    results: list[dict],
    k: int,
    chapter_to_section: dict[str, str],
    indent_depth_by_sid: dict[int, int],
    weights: dict[str, float] | None = None,
    max_options_per_question: int = DEFAULT_MAX_OPTIONS_PER_QUESTION,
    vector_threshold: float = 0.35,
) -> TermKPIs:
    weights = weights or DEFAULT_WEIGHTS
    top_k = results[:k]
    n = len(top_k)

    kpis = TermKPIs(term=term, k=k, n_results=n)
    if n == 0:
        # Total retrieval failure — strongest possible intercept signal. Vagueness
        # = 1.0 here so the composite reflects that instead of collapsing to 0.
        kpis.top_cosine = 0.0
        kpis.vagueness = 1.0
        kpis.composite = weights.get("vagueness", DEFAULT_WEIGHTS["vagueness"]) * 1.0
        return kpis

    codes = [r["goods_nomenclature_item_id"] for r in top_k]
    scores = [max(r.get("score", 0.0), 0.0) for r in top_k]

    # Per-level distinct sets and RRF-weighted distributions
    level_dists: dict[str, dict[str, float]] = {lvl: {} for lvl in LEVELS}
    for code, score in zip(codes, scores):
        for lvl in LEVELS:
            cut = _cut(code, lvl, chapter_to_section)
            if cut is None:
                continue
            level_dists[lvl][cut] = level_dists[lvl].get(cut, 0.0) + score

    for lvl in LEVELS:
        setattr(kpis, f"n_{lvl}", len(level_dists[lvl]))
        setattr(kpis, f"entropy_{lvl}", _entropy(level_dists[lvl]))

    # Inflexion / questions
    prev_count = 1
    questions_max = 0.0
    inflexion_levels = 0
    for lvl in LEVELS:
        curr = len(level_dists[lvl])
        if curr > prev_count:
            questions_max += math.log2(curr / prev_count)
            inflexion_levels += 1
        prev_count = curr if curr > 0 else prev_count
    kpis.questions_max = questions_max
    kpis.inflexion_levels = inflexion_levels

    # Total branching nodes in the candidate tree. Each branching node is one
    # "pick 1 of N" question for a smart LLM — so this is an UPPER BOUND on the
    # number of multi-option questions production InteractiveSearchService
    # would ask. A smarter LLM with cross-cutting questions could resolve fewer.
    children_by_parent: dict[tuple[str, str | None], set[str]] = {}
    for code in codes:
        parent: tuple[str, str | None] = ("root", None)
        for lvl in LEVELS:
            cut = _cut(code, lvl, chapter_to_section)
            children_by_parent.setdefault(parent, set()).add(cut or "?")
            parent = (lvl, cut)
    decision_points = 0
    widest_branch = 0
    for children in children_by_parent.values():
        if len(children) > 1:
            decision_points += 1
            if len(children) > widest_branch:
                widest_branch = len(children)
    kpis.decision_points = decision_points
    kpis.widest_branch = widest_branch

    # Worst-case multi-option questions = max number of branching nodes the
    # LLM would hit along ANY single root-to-leaf path. The LLM walks one
    # path to a single answer, so it only encounters forks on that path —
    # forks on parallel branches (taken by other candidates) don't cost it
    # questions. Production prompt forces multi-option questions, so each
    # fork on the walked path = 1 question.
    #
    # Total tree forks (decision_points) overestimates because it counts
    # forks on every branch. inflexion_levels overestimates too because it
    # counts levels with branching anywhere.
    log_cap = math.log2(max(2, max_options_per_question))
    worst_path_turns = 0
    worst_path_bits = 0.0
    for code in codes:
        turns_on_path = 0
        bits_on_path = 0.0
        parent: tuple[str, str | None] = ("root", None)
        for lvl in LEVELS:
            cut = _cut(code, lvl, chapter_to_section)
            width = len(children_by_parent.get(parent, set()))
            if width > 1:
                turns_on_path += math.ceil(math.log2(width) / log_cap)
                bits_on_path += math.log2(width)
            parent = (lvl, cut)
        if turns_on_path > worst_path_turns:
            worst_path_turns = turns_on_path
        if bits_on_path > worst_path_bits:
            worst_path_bits = bits_on_path
    kpis.worst_case_questions = worst_path_turns
    kpis.worst_case_bits = worst_path_bits

    # Total expected questions: entropy of the per-candidate RRF distribution.
    # This is the leaf-level entropy when each declarable code maps to its own
    # bucket; by chain rule equals the sum of per-level conditional entropies.
    kpis.questions_expected = _entropy({c: s for c, s in zip(codes, scores)})

    # Depth
    kpis.lca_digits = _longest_common_prefix(codes)
    kpis.unresolved_digits = 10 - kpis.lca_digits
    kpis.mean_indent_depth = (
        sum(indent_depth_by_sid.get(r["goods_nomenclature_sid"], 0) for r in top_k) / n
    )

    # Aux
    top_score, bottom_score = scores[0], scores[-1]
    kpis.top_score = top_score
    kpis.bottom_score = bottom_score
    kpis.score_flatness = (
        max(0.0, 1.0 - (top_score - bottom_score) / top_score) if top_score > 0 else 1.0
    )
    kpis.other_leaf_share = sum(1 for r in top_k if r.get("generation_type") == "ai") / n

    # Retrieval-failure signal: vagueness = hits_norm * cosine_norm
    # - hits_norm penalises low n_results relative to K (retrieval came back light)
    # - cosine_norm penalises top cosines near the threshold (retrieval came back weak)
    # Multiplied so a narrow well-defined query (low n_results + strong cosines, e.g.
    # "honey") doesn't get flagged, but a vague query (low n_results + barely-passing
    # cosines, e.g. "gift set") does.
    cosines = [r.get("cosine_score") for r in top_k if r.get("cosine_score") is not None]
    kpis.top_cosine = max(cosines) if cosines else 0.0
    hits_norm = max(0.0, 1.0 - n / k) if k > 0 else 0.0
    cosine_headroom = max(0.0, 1.0 - vector_threshold)
    cosine_norm = (
        max(0.0, min(1.0, 1.0 - (kpis.top_cosine - vector_threshold) / cosine_headroom))
        if cosine_headroom > 0 else 0.0
    )
    kpis.vagueness = hits_norm * cosine_norm

    # Diagnostics: dominant section/chapter and a one-line section chain
    if level_dists["section"]:
        sec_total = sum(level_dists["section"].values()) or 1.0
        top_sec = max(level_dists["section"].items(), key=lambda kv: kv[1])
        kpis.top_section = top_sec[0]
        chain_parts = sorted(level_dists["section"].items(), key=lambda kv: -kv[1])[:3]
        kpis.section_chain = " | ".join(f"{s} ({w/sec_total*100:.0f}%)" for s, w in chain_parts)
    if level_dists["chapter"]:
        chap_total = sum(level_dists["chapter"].values()) or 1.0
        top_chap = max(level_dists["chapter"].items(), key=lambda kv: kv[1])
        kpis.top_chapter = top_chap[0]
        kpis.top_chapter_share = top_chap[1] / chap_total

    # Composite — normalised pieces in [0,1]:
    section_spread_norm = (kpis.n_section - 1) / max(min(n, 21) - 1, 1)
    chapter_spread_norm = (kpis.n_chapter - 1) / max(min(n, 99) - 1, 1)
    questions_expected_norm = kpis.questions_expected / math.log2(n) if n > 1 else 0.0
    unresolved_norm = kpis.unresolved_digits / 10.0

    kpis.composite = (
        weights["section_spread"] * section_spread_norm
        + weights["chapter_spread"] * chapter_spread_norm
        + weights["questions_expected"] * questions_expected_norm
        + weights["unresolved_digits"] * unresolved_norm
        + weights["score_flatness"] * kpis.score_flatness
        + weights["other_leaf_share"] * kpis.other_leaf_share
        + weights.get("vagueness", 0.0) * kpis.vagueness
    )
    return kpis
