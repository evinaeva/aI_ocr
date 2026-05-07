"""
Phase 5: Expected-text validation.

Decision rule:
  PASS if compare_lines(ocr, expected, level="soft") returns pass=True.
  Line order does not matter; characters within each line must match
  exactly after `soft`-level normalization.

Levenshtein similarity is computed for evidence/UI only and does not
decide PASS.
"""
from __future__ import annotations

from app.normalizer import (
    normalize,
    compare_lines,
    _levenshtein_similarity,
)

# Kept for backward compatibility with consumers that still reference it.
SIMILARITY_THRESHOLD = 0.85


def normalize_for_similarity(text: str) -> str:
    """Backward-compatible alias — use `normalize(text, "soft")` directly."""
    return normalize(text or "", "soft")


def compute_similarity(ocr_text: str, expected_text: str) -> float:
    """Levenshtein similarity in [0.0, 1.0] on `soft`-normalized strings."""
    ocr_norm = normalize(ocr_text or "", "soft")
    exp_norm = normalize(expected_text or "", "soft")
    return _levenshtein_similarity(ocr_norm, exp_norm)


def build_validation_result(
    lang,
    zone_name,
    expected_texts,
    ocr_text,
    run_id,
):
    """Returns (validation_block, sim_or_None).

    The returned block always contains `validation_applied`. When validation
    is applied it also contains:
      - expected_text, normalized_ocr, normalized_expected
      - similarity (rounded), threshold (legacy 0.85, evidence only)
      - match_pass (bool), match_mode ("exact" | "line_multiset" | "none")
      - lines_ocr, lines_ref
    """
    from app.logging_utils import log_event

    if not lang:
        return {
            "validation_applied": False,
            "skip_reason": "lang_missing",
        }, None

    if (
        not expected_texts
        or not isinstance(expected_texts, dict)
        or lang not in expected_texts
        or not isinstance(expected_texts.get(lang), dict)
        or zone_name not in expected_texts[lang]
    ):
        return {
            "validation_applied": False,
            "skip_reason": "expected_text_missing",
        }, None

    expected_text = expected_texts[lang][zone_name]

    try:
        cmp = compare_lines(ocr_text or "", expected_text, level="soft")
        ocr_norm = normalize(ocr_text or "", "soft")[:2000]
        exp_norm = normalize(expected_text, "soft")[:2000]
        sim = float(cmp["similarity"])
        return {
            "validation_applied": True,
            "skip_reason": None,
            "expected_text": expected_text,
            "normalized_ocr": ocr_norm,
            "normalized_expected": exp_norm,
            "similarity": round(sim, 4),
            "threshold": SIMILARITY_THRESHOLD,
            "match_pass": bool(cmp["pass"]),
            "match_mode": cmp["mode"],
            "lines_ocr": cmp["lines_ocr"],
            "lines_ref": cmp["lines_ref"],
        }, sim
    except Exception:
        log_event(
            "similarity_error",
            run_id=run_id,
            zone_name=zone_name,
            error_type="similarity_exception",
        )
        return {
            "validation_applied": False,
            "skip_reason": "similarity_error",
        }, None
