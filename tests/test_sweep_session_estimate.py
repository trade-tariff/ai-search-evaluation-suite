"""Sweep session estimates derive from the real sweep grid.

A sweep request estimated a flat 500 sessions, unrelated to what the
sweep actually runs: every SWEEP_GRID combination under both strategies,
each over the request's personas and per-persona limit. The flat number
also let a stale override bypass the session cap entirely. The estimate
now scales the real combination count, keeps the uncapped per-persona
upper bound when limit is not set, and the session cap is enforced on
that correct bound.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "classification-evals"))

from fastapi import HTTPException

from backend import research_app_base as app
from backend.research_app_base import JobCreate
from classification_core.run_classify_matrix import SWEEP_GRID


class SweepSessionEstimateTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = {}
        for name in (
            "CLASSIFY_EVAL_SWEEP_SESSION_ESTIMATE",
            "CLASSIFY_EVAL_UNCAPPED_SESSION_ESTIMATE",
            "CLASSIFY_EVAL_MAX_SESSIONS",
            "CLASSIFY_EVAL_ALLOW_SWEEP",
        ):
            self._saved_env[name] = os.environ.get(name)
            os.environ.pop(name, None)
        self._saved_key = app._openai_api_key
        app._openai_api_key = lambda: "test-key"

    def tearDown(self):
        app._openai_api_key = self._saved_key
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_default_request_has_one_persona(self):
        # The API default is ONE persona, so a default sweep estimate is the
        # grid under both strategies for one persona's limit.
        self.assertEqual(JobCreate().personas, ["naive_vague"])

    def test_sweep_estimate_derives_from_the_real_grid(self):
        req = JobCreate(sweep=True, allow_spend=True)
        expected = 2 * len(SWEEP_GRID) * len(req.personas) * req.limit
        self.assertEqual(app._estimated_sessions(req), expected)
        # The grid is real, not a magic 16: 8 combinations x 2 strategies.
        self.assertEqual(len(SWEEP_GRID), 8)
        self.assertEqual(app._sweep_combo_count(), 16)

    def test_sweep_estimate_scales_with_personas_and_limit(self):
        req = JobCreate(sweep=True, allow_spend=True, personas=["naive_vague", "original"], limit=3)
        self.assertEqual(app._estimated_sessions(req), 2 * len(SWEEP_GRID) * 2 * 3)

    def test_removed_flat_override_no_longer_bypasses_the_estimate(self):
        os.environ["CLASSIFY_EVAL_SWEEP_SESSION_ESTIMATE"] = "1"
        req = JobCreate(sweep=True, allow_spend=True)
        self.assertEqual(app._estimated_sessions(req), 2 * len(SWEEP_GRID) * 1 * 5)

    def test_sweep_uncapped_accounts_per_persona_upper_bound(self):
        os.environ["CLASSIFY_EVAL_UNCAPPED_SESSION_ESTIMATE"] = "7"
        req = JobCreate(sweep=True, allow_spend=True, limit=None, personas=["naive_vague", "original"])
        self.assertEqual(app._estimated_sessions(req), 2 * len(SWEEP_GRID) * 2 * 7)

    def test_session_cap_enforced_on_the_correct_sweep_bound(self):
        os.environ["CLASSIFY_EVAL_ALLOW_SWEEP"] = "1"
        req = JobCreate(sweep=True, allow_spend=True)
        # Default request: 2 strategies x 8 grid combos x 1 persona x limit 5.
        self.assertEqual(app._estimated_sessions(req), 80)
        with self.assertRaises(HTTPException) as caught:
            app._validate_request(req)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn("Estimated sessions 80", caught.exception.detail)

        # At the correct bound the cap accepts the request.
        os.environ["CLASSIFY_EVAL_MAX_SESSIONS"] = "80"
        app._validate_request(req)

    def test_non_sweep_estimates_are_unchanged(self):
        self.assertEqual(app._estimated_sessions(JobCreate()), 5)
        self.assertEqual(
            app._estimated_sessions(JobCreate(limit=None)),
            500,
        )
        self.assertEqual(
            app._estimated_sessions(
                JobCreate(harness="e2e", question_modes="facet_rules,llm_generated")
            ),
            10,
        )


if __name__ == "__main__":
    unittest.main()
