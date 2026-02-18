"""
Text normalization for OCR vs reference comparison.
"""
import re
import unicodedata

# ── Placeholder whitelist ────────────────────────────────────────────────────
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

# For display: only remove constructs that are actual whitelisted placeholders
_ALL_PCT     = re.compile(r"%[^%]+%")

# Brand names to remove from OCR text
_BRAND_REMOVE_RE = re.compile(r"\bbongacams\b", re.IGNORECASE)


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
    """
    For display: remove whitelisted %placeholder%, [placeholder], <placeholder> constructs.
    NON-whitelisted angle/bracket constructs (e.g. <BUY TOKENS>, <PLAY NOW>) are KEPT
    so CTA text is visible in the Reference text column.
    """
    text = _PH_PCT.sub(
        lambda m: " " if _is_placeholder_name(m.group(1)) else m.group(0), text
    )
    text = _PH_BRACKET.sub(
        lambda m: " " if _is_placeholder_name(m.group(1)) else m.group(0), text
    )
    text = _PH_ANGLE.sub(
        lambda m: " " if _is_placeholder_name(m.group(1)) else m.group(0), text
    )
    # Also remove bare %...% (non-whitelisted %vars% still look ugly)
    text = _ALL_PCT.sub(" ", text)
    return text


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _pre_clean(text: str) -> str:
    """Remove emoji, arrows/bullets, BiDi marks; normalise dashes/spaces/quotes."""
    text = re.sub(
        "[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U00002B00-\U00002BFF\uFE00-\uFE0F]",
        "", text,
    )
    text = re.sub(
        r"[\u25B0-\u25FF\u27A0-\u27BF\u2190-\u21FF\u2022\u00B7]",
        "", text,
    )
    text = re.sub(r"[\u200E\u200F\u202A\u202B\u202C\u202D\u202E]", "", text)
    text = re.sub(r"[\xa0\u202f\u2009\u2007\u2008\u200a\u3000\u1680\u180e]", " ", text)
    text = re.sub(r"[\u2013\u2014\u2015\u2012\u2011]", "-", text)
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u00ab", '"').replace("\u00bb", '"')
    text = text.replace("\u2039", "'").replace("\u203a", "'")
    text = text.replace("\u00ad", "")
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    text = text.replace("\u2026", "...")
    return text


def remove_brand_names(text: str) -> str:
    return _BRAND_REMOVE_RE.sub("", text)


def normalize_strict(text: str) -> str:
    """Normalize for strict comparison (case-insensitive, no punctuation)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _pre_clean(text)
    text = remove_brand_names(text)
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = _collapse_whitespace(text)
    return text


def normalize_soft(text: str) -> str:
    """Same as normalize_strict but also removes whitelisted placeholder tokens."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _pre_clean(text)
    text = remove_brand_names(text)
    text = text.lower()
    text = _remove_placeholders(text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = _collapse_whitespace(text)
    return text


def clean_for_display(text: str) -> str:
    """
    Clean text for UI display.
    - Removes emoji, arrows, bullets, BiDi marks
    - Removes brand names
    - Removes only WHITELISTED placeholder tokens (e.g. %displayname%)
    - PRESERVES CTA text like <BUY TOKENS>, <PLAY NOW>, [here] when not whitelisted
    - Collapses extra blank lines
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


def has_placeholder(text: str) -> bool:
    """Return True if text contains at least one whitelisted placeholder token."""
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
