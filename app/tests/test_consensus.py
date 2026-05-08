"""
Test suite for app/pipeline/consensus.py.

The `low_confidence` MANUAL gate has been removed: zones are no longer
downgraded based on engine confidence alone. Phase 5 `match_pass` is
the sole text-content gate; engine confidence is preserved in evidence.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import unittest
from app.pipeline.ocr_dispatcher import ZoneEngineResult
from app.pipeline.consensus import resolve_consensus, normalize_for_consensus


def make_result(engine, text, confidence=None, error=None):
    return ZoneEngineResult(
        engine=engine,
        text=text,
        confidence=confidence,
        latency_ms=100.0,
        error=error,
    )


class TestNormalizeForConsensus(unittest.TestCase):

    def test_lowercase(self):
        self.assertEqual(normalize_for_consensus("Hello World"), "hello world")

    def test_collapse_whitespace(self):
        self.assertEqual(normalize_for_consensus("hello   world"), "hello world")

    def test_remove_punctuation(self):
        result = normalize_for_consensus("hello, world!")
        self.assertNotIn(",", result)
        self.assertNotIn("!", result)
        self.assertIn("hello", result)

    def test_backtick_preserved(self):
        result = normalize_for_consensus("`hello`")
        self.assertIn("`", result)

    def test_percent_preserved(self):
        result = normalize_for_consensus("%name%")
        self.assertIn("%", result)


class TestConsensus(unittest.TestCase):

    def test_no_engines_configured(self):
        result = resolve_consensus([], engines_configured=False)
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "no_engines_configured")
        self.assertIsNone(result["selected_engine"])
        self.assertIsNone(result["selected_text"])
        self.assertIsNone(result["rule_used"])

    def test_all_engines_failed(self):
        results = [
            make_result("google", "", error="engine_exception"),
            make_result("azure", "", error="engine_exception"),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "all_engines_failed")
        self.assertIsNone(result["selected_engine"])

    def test_majority_internal_confidence_wins(self):
        results = [
            make_result("google", "SAME TEXT", confidence=0.95),
            make_result("azure", "SAME TEXT", confidence=0.90),
            make_result("ocrspace", "DIFFERENT", confidence=0.85),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["selected_engine"], "google")
        self.assertEqual(result["zone_status"], "OK")

    def test_majority_no_confidence_lex_engine(self):
        results = [
            make_result("ocrspace", "SAME", confidence=None),
            make_result("azure", "SAME", confidence=None),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["selected_engine"], "azure")
        self.assertEqual(result["zone_status"], "OK")

    def test_disagreement_picks_longest_text(self):
        # Three different texts, no 2-of-3 agreement. After dropping
        # the best_confidence rule the picker falls through directly
        # to the longest-text deterministic fallback and returns
        # MANUAL `no_consensus`. Confidence is not consulted: only
        # Google reports it, so a confidence-based rule biased every
        # disagreement to Google.
        results = [
            make_result("google", "Hello", confidence=0.95),
            make_result("azure", "Helo World", confidence=0.72),
            make_result("ocrspace", "Hello Warld", confidence=0.80),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["rule_used"], "no_confidence_fallback")
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "no_consensus")
        # ocrspace text is longest after normalization; it wins.
        self.assertEqual(result["selected_engine"], "ocrspace")

    def test_no_confidence_fallback(self):
        results = [
            make_result("google", "Hello World", confidence=None),
            make_result("azure", "Hi", confidence=None),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["rule_used"], "no_confidence_fallback")
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "no_consensus")
        self.assertEqual(result["selected_text"], "Hello World")

    def test_tie_break_deterministic(self):
        results = [
            make_result("ocrspace", "same text", confidence=0.90),
            make_result("azure", "same text", confidence=0.90),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["selected_engine"], "azure")
        self.assertEqual(result["zone_status"], "OK")

    def test_single_engine_falls_through_to_no_consensus(self):
        # No best_confidence rule any more — and no low_confidence
        # MANUAL gate either. With one valid engine the result is
        # MANUAL via the longest-text fallback (rule_used =
        # no_confidence_fallback) because there is nothing to vote
        # against; downstream Phase 5 (`match_pass`) decides whether
        # the actual text matches the reference.
        results = [
            make_result("google", "Some text", confidence=0.65),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["selected_engine"], "google")
        self.assertEqual(result["rule_used"], "no_confidence_fallback")
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "no_consensus")

    def test_empty_string_valid(self):
        results = [
            make_result("google", "", confidence=0.95),
            make_result("azure", "", confidence=0.90),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["selected_text"], "")
        self.assertEqual(result["zone_status"], "OK")
        self.assertEqual(result["selected_engine"], "google")

    def test_confidence_out_of_range(self):
        results = [
            make_result("google", "Hello", confidence=1.5),
            make_result("azure", "World", confidence=None),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["rule_used"], "no_confidence_fallback")
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "no_consensus")


if __name__ == "__main__":
    unittest.main()
