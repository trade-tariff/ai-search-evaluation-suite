"""Live and saved views use the scoring rank order, not JSON emit order."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/product/backend"))

from benchmark import _extract_top_code
from judge import _extract_codes, evaluate_pair
from main import _serialise_run
from schemas import BenchmarkRun, CompletionResult


class LiveCodeRankingTest(unittest.TestCase):
    def test_confidence_ranking_is_shared_by_live_saved_and_scored_results(self):
        text = json.dumps({"answers": [
            {"commodity_code": "0101210000", "confidence": "Possible"},
            {"commodity_code": "6404199000", "confidence": " Strong "},
        ]})
        result = CompletionResult(model_id="fixture", prompt_index=1, response_text=text)
        baseline = CompletionResult(model_id="reference", prompt_index=1,
                                    response_text='{"answers":[{"commodity_code":"6404199000"}]}')
        self.assertEqual(_extract_top_code(text), "6404199000")
        self.assertEqual(_extract_top_code(text), _extract_codes(text)[0])
        self.assertTrue(evaluate_pair(baseline, result, "6404199000").gold_top1_match)
        run = BenchmarkRun(id="fixture", timestamp="fixture", model_results=[result],
                           baseline_results=[result], panel_results=[result], consensus_results=[result])
        payload = _serialise_run(run)
        for key in ("model_results", "baseline_results", "panel_results", "consensus_results"):
            self.assertEqual(payload[key][0]["top_code"], "6404199000")

    def test_equal_confidence_preserves_emission_order(self):
        text = json.dumps({"answers": [
            {"commodity_code": "0101210000", "confidence": "Good"},
            {"commodity_code": "6404199000", "confidence": "Good"},
        ]})
        self.assertEqual(_extract_top_code(text), "0101210000")

    def test_no_codes_has_no_top_code(self):
        for text in ("", "invalid JSON", '{"questions":[]}'):
            with self.subTest(text=text):
                self.assertIsNone(_extract_top_code(text))
