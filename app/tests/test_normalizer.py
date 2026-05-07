"""
Tests for the unified normalizer module.

Covers:
  - level semantics (`strict`, `soft`, `consensus`)
  - kept punctuation set for strict/soft (`!?".,$%&` only)
  - decoration brackets `<>[]` are stripped at strict/soft level
  - placeholder removal (whitelist only) at `soft` level
  - line-multiset PASS via `compare_lines`
  - Levenshtein similarity is evidence (does not gate PASS)
"""
import unittest

from app.normalizer import (
    normalize,
    normalize_strict,
    normalize_soft,
    compare_lines,
    has_placeholder,
)


class TestNormalizeLevels(unittest.TestCase):
    def test_strict_keeps_user_requested_punctuation(self):
        n = normalize("Hello, world! How's it? $5 100% A&B", "strict")
        for ch in (",", "!", ".", "?", "$", "%", "&"):
            self.assertIn(ch, n, f"strict must keep {ch!r}")

    def test_strict_drops_other_punctuation(self):
        n = normalize("a/b\\c|d:e;f", "strict")
        for ch in ("/", "\\", "|", ":", ";"):
            self.assertNotIn(ch, n, f"strict must drop {ch!r}")

    def test_strict_drops_decoration_brackets(self):
        # `[ ... ]` and `< ... >` around plain text are decoration; image OCR
        # never produces them, so they must not block PASS.
        n_sq = normalize("[ HESAB YARADIN ]", "strict")
        self.assertEqual(n_sq, "hesab yaradin")
        n_an = normalize("<BUY TOKENS>", "strict")
        self.assertEqual(n_an, "buy tokens")

    def test_strict_lowercases_and_collapses(self):
        self.assertEqual(normalize("Hello   World", "strict"), "hello world")

    def test_strict_normalizes_curly_quotes_and_dashes(self):
        n = normalize("“Buy now” — today", "strict")
        self.assertIn('"buy now"', n)
        self.assertNotIn("—", n)

    def test_strict_strips_emoji(self):
        self.assertEqual(normalize("Hello \U0001F600 world", "strict"), "hello world")

    def test_soft_removes_whitelisted_placeholders(self):
        n = normalize("Hi %username%! Buy <bonus_amount> tokens [first_name]", "soft")
        self.assertNotIn("%username%", n)
        self.assertNotIn("<bonus_amount>", n)
        self.assertNotIn("[first_name]", n)
        self.assertIn("hi", n)
        self.assertIn("buy", n)

    def test_soft_strips_non_whitelisted_brackets(self):
        # Whitelist is bypassed ("BUY TOKENS" is not a placeholder name)
        # but the surrounding brackets are still treated as decoration
        # and removed.
        n = normalize("Click <BUY TOKENS> now", "soft")
        self.assertNotIn("<", n)
        self.assertNotIn(">", n)
        self.assertIn("buy tokens", n)
        self.assertIn("click", n)
        self.assertIn("now", n)

    def test_consensus_strips_punctuation_keeps_placeholder_syntax(self):
        n = normalize("Hello, world! `code` %X% <Y> [Z]", "consensus")
        for ch in (",", "!"):
            self.assertNotIn(ch, n)
        for ch in ("`", "%", "<", ">", "[", "]"):
            self.assertIn(ch, n)

    def test_unknown_level_raises(self):
        with self.assertRaises(ValueError):
            normalize("x", "unknown")

    def test_aliases(self):
        self.assertEqual(normalize_strict("Hello!"), normalize("Hello!", "strict"))
        self.assertEqual(normalize_soft("Hi %username%"), normalize("Hi %username%", "soft"))

    def test_has_placeholder_whitelist_only(self):
        self.assertTrue(has_placeholder("Hi %username%"))
        self.assertTrue(has_placeholder("Buy <bonus_amount>"))
        self.assertFalse(has_placeholder("Click <BUY TOKENS>"))
        self.assertFalse(has_placeholder(""))


class TestCompareLines(unittest.TestCase):
    def test_exact_equal(self):
        r = compare_lines("Hello world", "Hello world", level="soft")
        self.assertTrue(r["pass"])
        self.assertEqual(r["mode"], "exact")

    def test_line_multiset_swapped_lines(self):
        ocr = "Buy tokens\nHello, world!"
        ref = "Hello, world!\nBuy tokens"
        r = compare_lines(ocr, ref, level="soft")
        self.assertTrue(r["pass"])
        self.assertEqual(r["mode"], "line_multiset")
        self.assertEqual(r["lines_ocr"], 2)
        self.assertEqual(r["lines_ref"], 2)

    def test_line_multiset_keeps_duplicates(self):
        # Multiset semantics: duplicates must be preserved on both sides.
        ocr = "A\nA\nB"
        ref = "A\nB"
        r = compare_lines(ocr, ref, level="soft")
        self.assertFalse(r["pass"])
        self.assertEqual(r["mode"], "none")

    def test_char_diff_within_line_fails(self):
        # OCR misreads one character: false PASS must NOT be produced.
        r = compare_lines("Hello world", "Hello vorld", level="soft")
        self.assertFalse(r["pass"])
        self.assertEqual(r["mode"], "none")

    def test_extra_punctuation_fails_at_strict(self):
        # Differing kept-set punctuation must produce MANUAL.
        r = compare_lines("Buy now!", "Buy now", level="strict")
        self.assertFalse(r["pass"])

    def test_decoration_brackets_do_not_block_pass(self):
        # Reference wraps text in `[ ... ]` (decoration only). OCR has
        # the bare text. With strict/soft policy this must PASS.
        r = compare_lines("HESAB YARADIN", "[ HESAB YARADIN ]", level="strict")
        self.assertTrue(r["pass"])
        self.assertEqual(r["mode"], "exact")

    def test_both_empty_pass(self):
        r = compare_lines("", "", level="soft")
        self.assertTrue(r["pass"])
        self.assertEqual(r["mode"], "exact")
        self.assertEqual(r["similarity"], 1.0)

    def test_one_empty_fails(self):
        r = compare_lines("", "hello", level="soft")
        self.assertFalse(r["pass"])

    def test_similarity_reported_for_evidence_when_failing(self):
        r = compare_lines("Hello world", "Hello vorld", level="soft")
        self.assertFalse(r["pass"])
        # 1-char diff in 11 chars → high similarity (>0.85), but PASS is
        # False because we no longer use the threshold.
        self.assertGreater(r["similarity"], 0.85)

    def test_unicode_dashes_normalized_to_ascii(self):
        r = compare_lines("a — b", "a - b", level="strict")
        self.assertTrue(r["pass"])


if __name__ == "__main__":
    unittest.main()
