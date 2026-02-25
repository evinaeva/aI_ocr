"""
Phase 5: Similarity computation for expected-text validation.

All logic is self-contained; do NOT import from normalizer.py.
"""
from __future__ import annotations

import re

SIMILARITY_THRESHOLD = 0.85

# Exact punctuation set from spec
_PUNCT_RE = re.compile(r'[!"#$&\'()*+,./:;=?@^_\{\|\}~\-]')

# Placeholder patterns
_PLACEHOLDER_RES = [
    re.compile(r'%[A-Za-z0-9_]+%'),
    re.compile(r'\[[A-Za-z0-9_]+\]'),
    re.compile(r'<[A-Za-z0-9_ ]+>'),
]


def normalize_for_similarity(text: str) -> str:
    """Normalize text for similarity comparison per Phase 5 spec."""
    if text is None:
        text = ""
    # 1. Lowercase
    text = text.lower()
    # 2. Collapse whitespace (Python \s+ covers Unicode whitespace)
    text = re.sub(r'\s+', ' ', text)
    # 3. Remove ASCII punctuation
    text = _PUNCT_RE.sub('', text)
    # 4. Remove placeholders
    for pat in _PLACEHOLDER_RES:
        text = pat.sub('', text)
    # 5. Strip
    return text.strip()


def _levenshtein_distance(a: str, b: str) -> int:
    """Rolling 2-row DP, O(min(n,m)) memory."""
    if len(a) < len(b):
        a, b = b, a
    # a is now the longer string
    n, m = len(a), len(b)
    prev = list(range(m + 1))
    curr = [0] * (m + 1)
    for i in range(1, n + 1):
        curr[0] = i
        for j in range(1, m + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + cost,
            )
        prev, curr = curr, prev
    return prev[m]


def compute_similarity(ocr_text: str, expected_text: str) -> float:
    """Compute Levenshtein similarity in [0.0, 1.0]."""
    ocr_norm = normalize_for_similarity(ocr_text)
    exp_norm = normalize_for_similarity(expected_text)

    # Truncate after normalization
    ocr_norm = ocr_norm[:2000]
    exp_norm = exp_norm[:2000]

    # Special cases
    if ocr_norm == '' and exp_norm == '':
        return 1.0
    if ocr_norm == '' or exp_norm == '':
        return 0.0

    distance = _levenshtein_distance(ocr_norm, exp_norm)
    max_len = max(len(ocr_norm), len(exp_norm))
    return 1.0 - (distance / max_len)


def build_validation_block(
    lang: str | None,
    zone_name: str,
    expected_texts: dict | None,
    ocr_text: str,
    run_id: str,
) -> dict:
    """
    Build the validation block for a zone.
    Always returns a dict with validation_applied key.
    May update zone_status/reason via returned side-channel.
    Returns (validation_block, new_status_override, new_reason_override)
    — caller applies status changes only when zone_status was 'OK'.
    """
    # This function only builds the block; caller handles status.
    # Kept separate for testability.
    raise NotImplementedError("Use build_validation_result instead")


def build_validation_result(
    lang: str | None,
    zone_name: str,
    expected_texts,
    ocr_text: str,
    run_id: str,
) -> tuple[dict, float | None]:
    """
    Returns (validation_block, similarity_float_or_None).
    similarity is unrounded float (for status decision); None if not computed.
    """
    from app.logging_utils import log_event

    # --- Skip: lang missing ---
    if not lang:
        return {
            "validation_applied": False,
            "skip_reason": "lang_missing",
        }, None

    # --- Skip: expected not available ---
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

    # --- Compute similarity ---
    try:
        sim = compute_similarity(ocr_text or "", expected_text)
        ocr_norm = normalize_for_similarity(ocr_text or "")[:2000]
        exp_norm = normalize_for_similarity(expected_text)[:2000]
        return {
            "validation_applied": True,
            "skip_reason": None,
            "expected_text": expected_text,
            "normalized_ocr": ocr_norm,
            "normalized_expected": exp_norm,
            "similarity": round(sim, 4),
            "threshold": SIMILARITY_THRESHOLD,
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
