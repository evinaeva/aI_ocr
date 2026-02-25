"""
Phase 5 tests for similarity.py.
"""
import unittest
from app.pipeline.similarity import (
    normalize_for_similarity,
    compute_similarity,
    build_validation_result,
    SIMILARITY_THRESHOLD,
)


class TestNormalize(unittest.TestCase):
    def test_identical_strings(self):
        self.assertEqual(compute_similarity("hello", "hello"), 1.0)

    def test_completely_different(self):
        # "abc" vs "xyz" — distance 3, max_len 3 → 0.0
        self.assertEqual(compute_similarity("abc", "xyz"), 0.0)

    def test_placeholder_percent_removed(self):
        n = normalize_for_similarity("Hello %username% world")
        self.assertNotIn("%username%", n)
        self.assertIn("hello", n)

    def test_placeholder_bracket_removed(self):
        n = normalize_for_similarity("Hi [first_name]!")
        self.assertNotIn("[first_name]", n)

    def test_placeholder_angle_removed(self):
        n = normalize_for_similarity("Buy <bonus_amount> tokens")
        self.assertNotIn("<bonus_amount>", n)

    def test_ascii_punctuation_removed(self):
        n = normalize_for_similarity("Hello, world! How's it?")
        self.assertNotIn(",", n)
        self.assertNotIn("!", n)
        self.assertNotIn("?", n)

    def test_both_empty_after_normalization(self):
        # Both become empty after placeholder removal
        self.assertEqual(compute_similarity("%a%", "%b%"), 1.0)

    def test_one_empty_after_normalization(self):
        self.assertEqual(compute_similarity("", "hello"), 0.0)
        self.assertEqual(compute_similarity("hello", ""), 0.0)

    def test_threshold_boundary_passes(self):
        # Build a pair with exactly 0.85 similarity
        # 20 chars, 3 substitutions → distance=3, max=20, sim=0.85
        a = "a" * 20
        b = "a" * 17 + "b" * 3
        sim = compute_similarity(a, b)
        # sim should be exactly 0.85
        self.assertAlmostEqual(sim, 0.85, places=10)
        # At exactly threshold — no downgrade
        self.assertGreaterEqual(sim, SIMILARITY_THRESHOLD)

    def test_threshold_boundary_fails(self):
        # 20 chars, 4 substitutions → distance=4, sim=0.8 < 0.85
        a = "a" * 20
        b = "a" * 16 + "b" * 4
        sim = compute_similarity(a, b)
        self.assertAlmostEqual(sim, 0.8, places=10)
        self.assertLess(sim, SIMILARITY_THRESHOLD)

    def test_rounding_in_json_not_in_comparison(self):
        # similarity value rounded to 4 decimals in block, unrounded for comparison
        a = "a" * 20
        b = "a" * 17 + "b" * 3
        block, sim_raw = build_validation_result(
            lang="en",
            zone_name="z",
            expected_texts={"en": {"z": b}},
            ocr_text=a,
            run_id="test-run",
        )
        self.assertTrue(block["validation_applied"])
        # JSON value is rounded to 4 decimals
        self.assertEqual(block["similarity"], round(sim_raw, 4))
        # Raw value used for comparison is full precision
        self.assertIsInstance(sim_raw, float)

    def test_unicode_emoji_no_crash(self):
        # Should not crash and produce stable output
        sim = compute_similarity("hello 🌟 world", "hello world")
        self.assertIsInstance(sim, float)
        self.assertGreaterEqual(sim, 0.0)
        self.assertLessEqual(sim, 1.0)

    def test_large_input_truncation(self):
        # Build 3000-char strings; normalization then truncation to 2000
        a = "a" * 3000
        b = "a" * 3000
        sim = compute_similarity(a, b)
        self.assertEqual(sim, 1.0)
        # Different beyond 2000 — should still work without OOM
        c = "a" * 2000 + "b" * 1000
        sim2 = compute_similarity(a, c)
        self.assertIsInstance(sim2, float)


class TestBuildValidationResult(unittest.TestCase):
    def test_lang_missing_skip(self):
        block, sim = build_validation_result(
            lang=None,
            zone_name="headline",
            expected_texts={"en": {"headline": "buy tokens"}},
            ocr_text="buy tokens",
            run_id="r1",
        )
        self.assertFalse(block["validation_applied"])
        self.assertEqual(block["skip_reason"], "lang_missing")
        self.assertIsNone(sim)

    def test_lang_empty_string_skip(self):
        block, sim = build_validation_result(
            lang="",
            zone_name="headline",
            expected_texts={"en": {"headline": "buy tokens"}},
            ocr_text="buy tokens",
            run_id="r1",
        )
        self.assertFalse(block["validation_applied"])
        self.assertEqual(block["skip_reason"], "lang_missing")

    def test_expected_text_missing_skip(self):
        block, sim = build_validation_result(
            lang="fr",
            zone_name="headline",
            expected_texts={"en": {"headline": "buy tokens"}},
            ocr_text="buy tokens",
            run_id="r1",
        )
        self.assertFalse(block["validation_applied"])
        self.assertEqual(block["skip_reason"], "expected_text_missing")

    def test_zone_not_in_expected_skip(self):
        block, sim = build_validation_result(
            lang="en",
            zone_name="footer",
            expected_texts={"en": {"headline": "buy tokens"}},
            ocr_text="buy tokens",
            run_id="r1",
        )
        self.assertFalse(block["validation_applied"])
        self.assertEqual(block["skip_reason"], "expected_text_missing")

    def test_computed_block_structure(self):
        block, sim = build_validation_result(
            lang="en",
            zone_name="headline",
            expected_texts={"en": {"headline": "buy tokens"}},
            ocr_text="buy tokens",
            run_id="r1",
        )
        self.assertTrue(block["validation_applied"])
        self.assertIsNone(block["skip_reason"])
        self.assertIn("expected_text", block)
        self.assertIn("normalized_ocr", block)
        self.assertIn("normalized_expected", block)
        self.assertIn("similarity", block)
        self.assertIn("threshold", block)
        self.assertEqual(block["threshold"], 0.85)
        self.assertEqual(sim, 1.0)

    def test_expected_none_skip(self):
        block, sim = build_validation_result(
            lang="en",
            zone_name="headline",
            expected_texts=None,
            ocr_text="buy tokens",
            run_id="r1",
        )
        self.assertFalse(block["validation_applied"])
        self.assertEqual(block["skip_reason"], "expected_text_missing")


class TestDowngradeLogic(unittest.TestCase):
    """Test the downgrade rules (applied in run_routes, tested here with helpers)."""

    def _apply_downgrade(self, zone_status, reason, sim):
        """Mirror the downgrade logic from run_routes."""
        if sim is not None and sim < SIMILARITY_THRESHOLD:
            if zone_status == "OK":
                return "MANUAL", "low_similarity"
        return zone_status, reason

    def test_ok_zone_downgraded_on_low_similarity(self):
        status, reason = self._apply_downgrade("OK", None, 0.7)
        self.assertEqual(status, "MANUAL")
        self.assertEqual(reason, "low_similarity")

    def test_ok_zone_not_downgraded_at_threshold(self):
        status, reason = self._apply_downgrade("OK", None, 0.85)
        self.assertEqual(status, "OK")
        self.assertIsNone(reason)

    def test_manual_zone_reason_preserved(self):
        # Already MANUAL with low_confidence — reason must not change
        status, reason = self._apply_downgrade("MANUAL", "low_confidence", 0.5)
        self.assertEqual(status, "MANUAL")
        self.assertEqual(reason, "low_confidence")

    def test_manual_zone_reason_preserved_no_sim(self):
        status, reason = self._apply_downgrade("MANUAL", "low_confidence", None)
        self.assertEqual(status, "MANUAL")
        self.assertEqual(reason, "low_confidence")


if __name__ == "__main__":
    unittest.main()
