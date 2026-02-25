"""
Test suite for app/pipeline/consensus.py

Covers all 9 mandatory cases from Phase 4 contract.
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
        # Backtick, %, <, > must NOT be removed
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

    # ── Case 1: No engines configured ────────────────────────────────────────
    def test_no_engines_configured(self):
        result = resolve_consensus([], engines_configured=False)
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "no_engines_configured")
        self.assertIsNone(result["selected_engine"])
        self.assertIsNone(result["selected_text"])
        self.assertIsNone(result["rule_used"])

    # ── Case 2: All engines failed ────────────────────────────────────────────
    def test_all_engines_failed(self):
        results = [
            make_result("google", "", error="engine_exception"),
            make_result("azure", "", error="engine_exception"),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "all_engines_failed")
        self.assertIsNone(result["selected_engine"])

    # ── Case 3: Majority ──────────────────────────────────────────────────────
    def test_majority(self):
        results = [
            make_result("google", "BUY TOKENS", confidence=0.95),
            make_result("azure", "BUY TOKENS", confidence=0.90),
            make_result("ocrspace", "BUY TOKENX", confidence=0.80),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["rule_used"], "majority")
        self.assertEqual(result["selected_text"], "BUY TOKENS")
        self.assertEqual(result["zone_status"], "OK")

    # ── Case 4: Best confidence ───────────────────────────────────────────────
    def test_best_confidence(self):
        results = [
            make_result("google", "Hello World", confidence=0.95),
            make_result("azure", "Helo World", confidence=0.72),
            make_result("ocrspace", "Hello Warld", confidence=0.80),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["rule_used"], "best_confidence")
        self.assertEqual(result["selected_engine"], "google")
        self.assertEqual(result["zone_status"], "OK")

    # ── Case 5: Fallback (no_confidence_fallback) ─────────────────────────────
    def test_no_confidence_fallback(self):
        results = [
            make_result("google", "Hello World", confidence=None),
            make_result("azure", "Hi", confidence=None),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["rule_used"], "no_confidence_fallback")
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "no_consensus")
        # Longest text wins
        self.assertEqual(result["selected_text"], "Hello World")

    # ── Case 6: Tie-break deterministic ──────────────────────────────────────
    def test_tie_break_deterministic(self):
        # Two engines with same text, same confidence → alphabetically first engine
        results = [
            make_result("ocrspace", "same text", confidence=0.90),
            make_result("azure", "same text", confidence=0.90),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["rule_used"], "majority")
        # azure < ocrspace alphabetically
        self.assertEqual(result["selected_engine"], "azure")

    # ── Case 7: Low confidence ────────────────────────────────────────────────
    def test_low_confidence(self):
        results = [
            make_result("google", "Some text", confidence=0.65),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "low_confidence")
        self.assertEqual(result["selected_engine"], "google")

    # ── Case 8: Empty string is valid candidate ───────────────────────────────
    def test_empty_string_valid(self):
        # Empty string with valid confidence participates in selection
        results = [
            make_result("google", "", confidence=0.95),
            make_result("azure", "", confidence=0.90),
        ]
        result = resolve_consensus(results, engines_configured=True)
        self.assertEqual(result["rule_used"], "majority")
        self.assertEqual(result["selected_text"], "")
        self.assertEqual(result["zone_status"], "OK")

    # ── Case 9: Confidence out of range treated as None ───────────────────────
    def test_confidence_out_of_range(self):
        # confidence=1.5 is outside [0.0, 1.0] → treated as None
        results = [
            make_result("google", "Hello", confidence=1.5),
            make_result("azure", "World", confidence=None),
        ]
        result = resolve_consensus(results, engines_configured=True)
        # No valid confidence → fallback
        self.assertEqual(result["rule_used"], "no_confidence_fallback")
        self.assertEqual(result["zone_status"], "MANUAL")
        self.assertEqual(result["reason"], "no_consensus")


if __name__ == "__main__":
    unittest.main()
