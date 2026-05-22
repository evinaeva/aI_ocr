"""Tests for the EN-anchor translator outlier check."""
import pytest

from app.pipeline.translator_check import (
    TranslatorOutlier,
    extract_numbers,
    find_translator_outliers,
)


# ── extract_numbers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Only until June 13", ["13"]),
        ("Win 5,000 tokens", ["5000"]),
        ("Cost: 1.234,56 EUR", ["123456"]),
        ("13 июня — 50%", ["13", "50"]),
        ("No numbers here", []),
        ("", []),
        (None, []),
        ("Multiple 1 occurrences 1 same number 1", ["1", "1", "1"]),
        ("13 13 13", ["13", "13", "13"]),
    ],
)
def test_extract_numbers(text, expected):
    assert extract_numbers(text) == expected


# ── find_translator_outliers — match cases ───────────────────────────────────

def test_no_outlier_when_numbers_identical():
    """Same numbers, different surrounding text → no outlier."""
    en = "Only until June 13"
    ru = "Только до 13 июня"
    assert find_translator_outliers(en, ru) is None


def test_no_outlier_when_multiplicity_differs_but_set_equal():
    """We compare sets — `13 13` matches `13` (both contain that fact)."""
    en = "13"
    lang = "13 13 13"
    assert find_translator_outliers(en, lang) is None


def test_no_outlier_when_both_empty():
    assert find_translator_outliers("", "") is None
    assert find_translator_outliers("No numbers", "Без чисел") is None


def test_separator_normalisation_5000_vs_5_comma_000():
    en = "Up to 5,000 tokens"
    lang = "Hasta 5000 tokens"
    assert find_translator_outliers(en, lang) is None


# ── find_translator_outliers — mismatch cases ────────────────────────────────

def test_outlier_tr_1_vs_en_13():
    """The actual TR case from 1.zip."""
    en = "Only until June 13"
    tr = "Sadece 1 Haziran'a kadar"
    out = find_translator_outliers(en, tr)
    assert out is not None
    assert out.has_mismatch is True
    assert out.missing_in_lang == ["13"]
    assert out.extra_in_lang == ["1"]
    assert out.reason_code() == "translator_outlier_numbers"


def test_outlier_tooltip_has_both_lists():
    out = find_translator_outliers("13 50%", "1 60%")
    assert out is not None
    tip = out.tooltip("tr")
    assert "missing" in tip
    assert "extra" in tip
    assert "13" in tip
    assert "1" in tip
    assert "tr" in tip


def test_outlier_only_missing_no_extra():
    """EN has a fact lang dropped entirely."""
    out = find_translator_outliers("June 13, 50% off", "Iyun, off")
    assert out is not None
    assert "13" in out.missing_in_lang
    assert "50" in out.missing_in_lang
    assert out.extra_in_lang == []


def test_extra_numbers_only_is_not_flagged():
    """`extra_in_lang` alone is NOT a mismatch — it's often locale convention.

    Examples: Japanese/Korean dates inline numeric month
    ('6月13日' has `6` and `13`, while EN 'June 13' has only `13`).
    """
    assert find_translator_outliers("Only June 13", "6月13日まで") is None
    assert find_translator_outliers("Buy tokens", "Buy 10 tokens") is None


def test_tooltip_shows_extras_when_present_with_missing():
    """When there's a missing number, the tooltip also mentions extras."""
    out = find_translator_outliers("13 only", "1 something")
    assert out is not None
    assert "missing" in out.tooltip("xx")
    assert "extra" in out.tooltip("xx")
    assert "13" in out.tooltip("xx")
    assert "1" in out.tooltip("xx")
