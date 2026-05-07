"""
Text normalization and comparison for OCR vs reference text.

Single source of truth used by:
  - Phase 4 consensus  (level="consensus")
  - Phase 5 similarity (level="soft")
  - section_matcher    (level="strict" / "soft")

Three normalization levels:
  - "strict"    — full unicode cleanup, lowercase, keeps `! ? " . , $ % &`.
                  Everything else outside word characters and whitespace
                  is replaced by space. No placeholder removal.
                  `<`, `>`, `[`, `]` are dropped as decoration: text such
                  as `[ HESAB YARADIN ]` becomes `hesab yaradin`. Whitelist
                  placeholders (e.g. <displayname>) are still detected by
                  the placeholder removal pass at `soft` level before this
                  strip runs.
  - "soft"      — same as strict, but whitelisted placeholder tokens
                  (e.g. %displayname%, [username], <skin>) are removed
                  before the punctuation strip.
  - "consensus" — minimal cleanup for engine-vs-engine matching;
                  byte-equivalent to the previous Phase 4 implementation.
                  Lowercase + collapse whitespace + strip a fixed set of
                  ASCII punctuation, preserving backtick, %, <, >, [, ].

`compare_lines(ocr, ref, level)` is the canonical PASS/MANUAL primitive.
PASS if either:
  (a) the joined normalized strings are equal (line order preserved), or
  (b) the multiset of non-empty normalized lines is equal
      (line order does NOT matter; each line still has to match
       exactly character-by-character after normalization).
Otherwise the comparison fails and the caller emits MANUAL.

Levenshtein similarity is returned for evidence/UI; it does NOT decide PASS.
"""
from __future__ import annotations

import re
import unicodedata
from typing import List

# ── Placeholder whitelist ───────────────────────────────────────
# Only these known variable names are treated as placeholders.
# CTA text like <BUY TOKENS> or <PLAY NOW> is NOT a placeholder.
_PLACEHOLDER_NAMES = frozenset({
    "skin",
    "displayname",
    "username",
    "subscriber_firstname_capitalized",
    "first_name",
    "bonus_amount",
    "date",
})

_PH_PCT     = re.compile(r"%([^%]+)%")
_PH_BRACKET = re.compile(r"\[([^\]]+)\]")
_PH_ANGLE   = re.compile(r"<([^>]+)>")
_ALL_PCT    = re.compile(r"%[^%]+%")

_BRAND_REMOVE_RE = re.compile(r"\bbongacams\b", re.IGNORECASE)

# ── Punctuation policy ────────────────────────────────────────
# strict/soft keep `! ? " . , $ % &`. The decoration brackets `< > [ ]`
# are stripped because in marketing copy they're layout noise around
# the actual phrase (e.g. `[ HESAB YARADIN ]`) — the OCR will never
# capture them, and we don't want operators to chase phantom
# differences. Whitelist placeholders (`<displayname>`, `[username]`,
# `%bonus_amount%`) are matched and removed BEFORE this strip runs at
# `soft` level, so they still vanish correctly.
_STRIP_STRICT_RE = re.compile(r'[^\w\s!?".,$%&]', re.UNICODE)

# consensus: byte-equivalent to old normalize_for_consensus.
# Strips this fixed ASCII set; preserves backtick, %, <, >, [, ].
_CONSENSUS_PUNCT_RE = re.compile(r'[!"#$&\'()*+,./:;=?@^_{}|~\-]')


def _is_placeholder_name(name: str) -> bool:
    return name.strip().lower() in _PLACEHOLDER_NAMES


def _remove_placeholders(text: str) -> str:
    """Remove only whitelisted placeholder tokens; leave everything else intact."""
    text = _PH_PCT.sub(
        lambda m: " " if _is_placeholder_name(m.group(1)) else m.group(0), text
    )
    text = _PH_BRACKET.sub(
        lambda m: " " if _is_placeholder_name(m.group(1)) else m.group(0), text
    )
    text = _PH_ANGLE.sub(
        lambda m: " " if _is_placeholder_name(m.group(1)) else m.group(0), text
    )
    return text


def _remove_placeholders_for_display(text: str) -> str:
    text = _PH_PCT.sub(
        lambda m: " " if _is_placeholder_name(m.group(1)) else m.group(0), text
    )
    text = _PH_BRACKET.sub(
        lambda m: " " if _is_placeholder_name(m.group(1)) else m.group(0), text
    )
    text = _PH_ANGLE.sub(
        lambda m: " " if _is_placeholder_name(m.group(1)) else m.group(0), text
    )
    text = _ALL_PCT.sub(" ", text)
    return text


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _pre_clean(text: str) -> str:
    """Remove emoji, arrows/bullets, BiDi marks; normalise dashes/spaces/quotes."""
    text = re.sub(
        "[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U00002B00-\U00002BFF︀-️]",
        "", text,
    )
    text = re.sub(
        r"[▰-◿➠-➿←-⇿•·]",
        "", text,
    )
    text = re.sub(r"[‎‏‪‫‬‭‮]", "", text)
    text = re.sub(r"[\xa0     　 ᠎]", " ", text)
    text = re.sub(r"[–—―‒‑]", "-", text)
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("«", '"').replace("»", '"')
    text = text.replace("‹", "'").replace("›", "'")
    text = text.replace("­", "")
    text = re.sub(r"[​‌‍﻿]", "", text)
    text = text.replace("…", "...")
    return text


def remove_brand_names(text: str) -> str:
    return _BRAND_REMOVE_RE.sub("", text)


def normalize(text: str, level: str = "strict") -> str:
    """Canonical character-level normalization at one of three levels.

    Returns "" for empty/None input. See module docstring for level semantics.
    """
    if not text:
        return ""

    if level == "consensus":
        t = text.lower()
        t = re.sub(r"\s+", " ", t)
        t = _CONSENSUS_PUNCT_RE.sub("", t)
        return t.strip()

    if level not in ("strict", "soft"):
        raise ValueError(f"unknown normalization level: {level!r}")

    t = unicodedata.normalize("NFC", text)
    t = _pre_clean(t)
    t = remove_brand_names(t)
    t = t.lower()
    if level == "soft":
        t = _remove_placeholders(t)
    t = _STRIP_STRICT_RE.sub(" ", t)
    return _collapse_whitespace(t)


# ── Backward-compatible aliases ─────────────────────────────────

def normalize_strict(text: str) -> str:
    return normalize(text, "strict")


def normalize_soft(text: str) -> str:
    return normalize(text, "soft")


def has_placeholder(text: str) -> bool:
    """Return True if text contains at least one whitelisted placeholder token."""
    if not text:
        return False
    for m in _PH_PCT.finditer(text):
        if _is_placeholder_name(m.group(1)):
            return True
    for m in _PH_BRACKET.finditer(text):
        if _is_placeholder_name(m.group(1)):
            return True
    for m in _PH_ANGLE.finditer(text):
        if _is_placeholder_name(m.group(1)):
            return True
    return False


def clean_for_display(text: str) -> str:
    """Clean text for UI display.

    - Removes emoji, arrows, bullets, BiDi marks, brand names.
    - Removes only whitelisted placeholder tokens.
    - Preserves CTA text like <BUY TOKENS>, <PLAY NOW>, [here].
    - Collapses extra blank lines.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _pre_clean(text)
    text = remove_brand_names(text)
    text = _remove_placeholders_for_display(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line.strip())
    return text.strip()


# ── Line-aware PASS comparator ─────────────────────────────────

_LINE_SPLIT_RE = re.compile(r"[\r\n]+")


def _normalize_lines(text: str, level: str) -> List[str]:
    if not text:
        return []
    out: List[str] = []
    for chunk in _LINE_SPLIT_RE.split(text):
        n = normalize(chunk, level)
        if n:
            out.append(n)
    return out


def compare_lines(ocr: str, ref: str, level: str = "soft") -> dict:
    """PASS/MANUAL primitive. See module docstring."""
    full_ocr = normalize(ocr or "", level)
    full_ref = normalize(ref or "", level)

    sim = _levenshtein_similarity(full_ocr[:2000], full_ref[:2000])

    ocr_lines = _normalize_lines(ocr or "", level)
    ref_lines = _normalize_lines(ref or "", level)

    if not full_ocr and not full_ref:
        return {
            "pass": True,
            "mode": "exact",
            "similarity": 1.0,
            "lines_ocr": len(ocr_lines),
            "lines_ref": len(ref_lines),
        }

    if full_ocr == full_ref:
        return {
            "pass": True,
            "mode": "exact",
            "similarity": sim,
            "lines_ocr": len(ocr_lines),
            "lines_ref": len(ref_lines),
        }

    if ocr_lines and sorted(ocr_lines) == sorted(ref_lines):
        return {
            "pass": True,
            "mode": "line_multiset",
            "similarity": sim,
            "lines_ocr": len(ocr_lines),
            "lines_ref": len(ref_lines),
        }

    return {
        "pass": False,
        "mode": "none",
        "similarity": sim,
        "lines_ocr": len(ocr_lines),
        "lines_ref": len(ref_lines),
    }


# ── Levenshtein (evidence only; PASS does not depend on it) ──────────────

def _levenshtein_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    n, m = len(a), len(b)
    if m == 0:
        return n
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


def _levenshtein_similarity(a: str, b: str) -> float:
    a = (a or "")[:2000]
    b = (b or "")[:2000]
    if a == "" and b == "":
        return 1.0
    if a == "" or b == "":
        return 0.0
    distance = _levenshtein_distance(a, b)
    max_len = max(len(a), len(b))
    return 1.0 - (distance / max_len)
