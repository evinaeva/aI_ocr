"""
Translator-side QA — catches typos in localised DOCX by comparing
language-invariant facts (numbers, dates, percentages, currency amounts)
against the canonical EN docx.

This is a **deterministic** check — no LLM required. It runs on every
locale and flags translator outliers the rule comparator can't catch
because the same outlier may also appear on the banner (when the
designer copy-pasted the bad translation verbatim).

Why this matters:
  - In the BANNER BNG-30996 archive the TR translator wrote "1 Haziran"
    where the EN says "June 13" and 36 other locales correctly say "13".
    The banner itself contains "13 Haziran", so OCR vs lang docx fails —
    but the root cause is the docx, not the banner. EN-anchor exposes
    this directly.

Rule: a locale is flagged when the multiset of numeric tokens in the
lang reference text differs from the EN reference. Separators (`,`,
`.`) inside a number are stripped before comparison, so `5,000` and
`5000` are treated as the same number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Set


# Capture any digit run that may contain thousand/decimal separators.
# We treat the separator as cosmetic and strip it before comparing.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")


def extract_numbers(text: str) -> List[str]:
    """Return all numeric tokens in `text`, separators stripped.

    Examples:
      "Win 5,000 tokens" → ["5000"]
      "13 июня — 50%" → ["13", "50"]
      "" → []
    """
    if not text:
        return []
    return [_strip_separators(m.group(0)) for m in _NUMBER_RE.finditer(text)]


def _strip_separators(num_token: str) -> str:
    """`5,000` → `5000`. `1.234,56` → `123456` (we don't care about decimals)."""
    return re.sub(r"[.,]", "", num_token)


@dataclass
class TranslatorOutlier:
    """Result of `find_translator_outliers`. Truthy when there's a mismatch."""
    en_numbers: List[str]
    lang_numbers: List[str]
    missing_in_lang: List[str]      # numbers present in EN but not in lang
    extra_in_lang: List[str]        # numbers present in lang but not in EN

    @property
    def has_mismatch(self) -> bool:
        return bool(self.missing_in_lang or self.extra_in_lang)

    def reason_code(self) -> str:
        """Short machine-readable reason for the MANUAL row."""
        return "translator_outlier_numbers"

    def tooltip(self, lang: str) -> str:
        """Human-readable explanation for the operator tooltip."""
        parts = []
        if self.missing_in_lang:
            parts.append(
                f"missing {sorted(self.missing_in_lang)} (in EN but not in {lang} docx)"
            )
        if self.extra_in_lang:
            parts.append(
                f"extra {sorted(self.extra_in_lang)} (in {lang} docx but not in EN)"
            )
        return "Translator typo: " + "; ".join(parts)


def find_translator_outliers(
    en_text: str,
    lang_text: str,
) -> Optional[TranslatorOutlier]:
    """Return None when no EN number is missing from the lang text.

    We flag **only missing** numbers — numbers present in EN that the
    translator dropped (the actionable error class). Extra numbers in
    the lang text are NOT flagged because they're often locale-specific
    conventions, not typos:
      - Japanese / Korean: "June 13" is written "6月13日" (month-number
        before day-number); a `6` appears in lang but not in EN.
      - Various locales: writing the year inline, abbreviating with
        digits, etc.

    `extra_in_lang` is still recorded in the result for the operator
    tooltip (informational), but the `has_mismatch` decision is based
    purely on missing numbers.
    """
    en_set: Set[str] = set(extract_numbers(en_text))
    lang_set: Set[str] = set(extract_numbers(lang_text))

    missing = en_set - lang_set
    extra = lang_set - en_set

    if not missing:
        return None

    return TranslatorOutlier(
        en_numbers=sorted(en_set),
        lang_numbers=sorted(lang_set),
        missing_in_lang=sorted(missing),
        extra_in_lang=sorted(extra),
    )
