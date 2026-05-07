"""
Phase 5 tests for similarity.py.

Punctuation policy update:
  Strict/soft normalization keeps `! ? " . , $ % &` per spec.
  Tests assert preservation of these characters.

Downgrade rule update:
  An OK zone is downgraded to MANUAL when `match_pass` is False
  (reason `no_text_match`). The 0.85 Levenshtein threshold is kept
  in the validation block as evidence only.
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

    def test_user_punctuation_preserved(self):
        n = normalize_for_similarity("Hello, world! How's it? $5 100% A&B \"quote\".")
        for ch in (",", "!", "?", ".", "$", "%", "&", '"'):
            self.assertIn(ch, n, f"expected {ch!r} preserved in soft normalization")

    def test_other_punctuation_dropped(self):
        n = normalize_for_similarity("a/b\\c|d:e;f(g)h*i+j")
        for ch in ("/", "\\", "|", ":", ";", "(", ")", "*", "+"):
            self.assertNotIn(ch, n, f"expected {ch!r} dropped")

    def test_both_empty_after_normalization(self):
        self.assertEqual(compute_similarity("%username%", "%username%"), 1.0)

    def test_one_empty_after_normalization(self):
        self.assertEqual(compute_similarity("", "hello"), 0.0)
        self.assertEqual(compute_similarity("hello", ""), 0.0)

    def test_threshold_constant_preserved(self):
        self.assertEqual(SIMILARITY_THRESHOLD, 0.85)

    def test_unicode_emoji_no_crash(self):
        sim = compute_similarity("hello \U0001f31f world", "hello world")
        self.assertIsInstance(sim, float)
        self.assertGreaterEqual(sim, 0.0)
        self.assertLessEqual(sim, 1.0)

    def test_large_input_truncation(self):
        a = "a" * 3000
        b = "a" * 3000
        sim = compute_similarity(a, b)
        self.assertEqual(sim, 1.0)
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
        for k in (
            "expected_text",
            "normalized_ocr",
            "normalized_expected",
            "similarity",
            "threshold",
            "match_pass",
            "match_mode",
            "lines_ocr",
            "lines_ref",
        ):
            self.assertIn(k, block, f"missing key {k!r}")
        self.assertEqual(block["threshold"], 0.85)
        self.assertTrue(block["match_pass"])
        self.assertEqual(block["match_mode"], "exact")
        self.assertEqual(sim, 1.0)

    def test_line_order_does_not_affect_pass(self):
        # Two-line reference, OCR returns the same lines reversed — must PASS.
        block, sim = build_validation_result(
            lang="en",
            zone_name="banner",
            expected_texts={"en": {"banner": "Hello, world!\nBuy tokens."}},
            ocr_text="Buy tokens.\nHello, world!",
            run_id="r1",
        )
        self.assertTrue(block["validation_applied"])
        self.assertTrue(block["match_pass"])
        self.assertEqual(block["match_mode"], "line_multiset")

    def test_char_diff_does_not_pass(self):
        block, sim = build_validation_result(
            lang="en",
            zone_name="banner",
            expected_texts={"en": {"banner": "Hello world"}},
            ocr_text="Hello vorld",
            run_id="r1",
        )
        self.assertTrue(block["validation_applied"])
        self.assertFalse(block["match_pass"])
        self.assertEqual(block["match_mode"], "none")

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
    """Mirrors the run_routes downgrade rule: OK → MANUAL when match_pass is False."""

    def _apply_downgrade(self, zone_status, reason, *, validation_applied, match_pass):
        if (
            validation_applied is True
            and match_pass is False
            and zone_status == "OK"
        ):
            return "MANUAL", "no_text_match"
        return zone_status, reason

    def test_ok_zone_downgraded_when_no_match(self):
        status, reason = self._apply_downgrade(
            "OK", None, validation_applied=True, match_pass=False
        )
        self.assertEqual(status, "MANUAL")
        self.assertEqual(reason, "no_text_match")

    def test_ok_zone_not_downgraded_when_pass(self):
        status, reason = self._apply_downgrade(
            "OK", None, validation_applied=True, match_pass=True
        )
        self.assertEqual(status, "OK")
        self.assertIsNone(reason)

    def test_ok_zone_not_downgraded_when_validation_skipped(self):
        status, reason = self._apply_downgrade(
            "OK", None, validation_applied=False, match_pass=False
        )
        self.assertEqual(status, "OK")
        self.assertIsNone(reason)

    def test_manual_zone_reason_preserved(self):
        status, reason = self._apply_downgrade(
            "MANUAL", "low_confidence", validation_applied=True, match_pass=False
        )
        self.assertEqual(status, "MANUAL")
        self.assertEqual(reason, "low_confidence")


if __name__ == "__main__":
    unittest.main()
