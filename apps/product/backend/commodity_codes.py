"""Canonical commodity-code handling, shared by every scoring surface.

There were three near-identical copies of this normalisation (the E2E matrix,
the retrieval experiments, the local DB helper) and they did not agree on the
empty case - one returned the original string where the others returned "".
Model Comparison had no normalisation at all, so a code written
"6912 00 23 10" scored as a miss there while matching in Gold Eval - E2E. Two
harnesses disagreeing about whether an answer is correct is the worst possible
kind of divergence in an evaluation tool, so there is now one implementation.
"""

from __future__ import annotations

import re

CODE_LENGTH = 10


def flat_code(code: str | None) -> str:
    """Digits only, right-padded to 10, truncated to 10. "" when no digits.

    Padding matters: the tariff treats a code as a prefix path, so an 8-digit
    answer is the 10-digit code ending "00". Truncation guards against
    concatenated or over-long junk.
    """
    digits = re.sub(r"\D", "", code or "")
    return digits.ljust(CODE_LENGTH, "0")[:CODE_LENGTH] if digits else ""


def is_well_formed(code: str | None) -> bool:
    """Was this a full 10-digit code BEFORE padding?

    flat_code() happily pads "691200" to "6912000000", which is a real but
    different code. Callers that need to distinguish malformed model output
    from a genuine wrong answer must ask this first.
    """
    digits = re.sub(r"\D", "", code or "")
    return len(digits) == CODE_LENGTH


def rank_of(codes: list[str], expected: str | None) -> int | None:
    """1-based rank of `expected` within `codes`, or None if absent.

    Compares normalised, so formatting differences never decide correctness.
    """
    exp = flat_code(expected)
    if not exp:
        return None
    for idx, code in enumerate(codes, start=1):
        if flat_code(code) == exp:
            return idx
    return None


def common_prefix_len(a: str | None, b: str | None) -> int:
    """Length of the shared leading digits of two normalised codes.

    2 = chapter, 4 = heading, 6 = subheading, 8 = commodity, 10 = full.
    """
    fa, fb = flat_code(a), flat_code(b)
    if not fa or not fb:
        return 0
    n = 0
    for ca, cb in zip(fa, fb):
        if ca != cb:
            break
        n += 1
    return n
