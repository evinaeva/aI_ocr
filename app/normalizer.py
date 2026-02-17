"""
Text normalization for OCR vs reference comparison.
"""
import re
import unicodedata

# ── Placeholder whitelist ────────────────────────────────────────────────────
# Only these known variable names are treated as placeholders and removed
# during soft normalization / has_placeholder check.
# Everything else (CTA buttons like <BUY TOKENS>, UI labels like [here]) is kept
# for comparison but stripped from display.
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

# For display: strip ALL angle/bracket/percent constructs regardless of whitelist
_ALL_ANGLE   = re.compile(r"<[^>]*>")
_ALL_BRACKET = re.compile(r"\[[^\]]*\]")
_ALL_PCT     = re.compile(r"%[^%]+%")

# Brand names to remove from OCR text (should not affect matching)
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


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _pre_clean(text: str) -> str:
    """
    Pre-clean: remove emoji, arrows/bullets, BiDi marks;
    normalise dashes, spaces, and quotes to ASCII equivalents.
    """
    # Emoji & pictographic ranges
    text = re.sub(
        "[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U00002B00-\U00002BFF\uFE00-\uFE0F]",
        "", text,
    )
    # Geometric shapes (U+25B0-U+25FF), dingbat arrows (U+27A0-U+27BF),
    # standard arrows (U+2190-U+21FF), bullet / middle dot
    text = re.sub(
        r"[\u25B0-\u25FF\u27A0-\u27BF\u2190-\u21FF\u2022\u00B7]",
        "", text,
    )
    # BiDi / LTR-RTL embedding marks
    text = re.sub(r"[\u200E\u200F\u202A\u202B\u202C\u202D\u202E]", "", text)
    # Unicode spaces -> regular space
    text = re.sub(r"[\xa0\u202f\u2009\u2007\u2008\u200a\u3000\u1680\u180e]", " ", text)
    # Dashes -> hyphen
    text = re.sub(r"[\u2013\u2014\u2015\u2012\u2011]", "-", text)
    # Smart quotes -> straight
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u00ab", '"').replace("\u00bb", '"')
    text = text.replace("\u2039", "'").replace("\u203a", "'")
    # Soft hyphen, zero-width chars
    text = text.replace("\u00ad", "")
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    # Ellipsis character -> three dots
    text = text.replace("\u2026", "...")
    return text


def remove_brand_names(text: str) -> str:
    """Remove brand names (BongaCams etc.) that should not affect matching."""
    return _BRAND_REMOVE_RE.sub("", text)


def normalize_strict(text: str) -> str:
    """
    Normalize for strict comparison.
    Pipeline: NFC -> pre_clean -> remove brand names -> lowercase
              -> strip punctuation -> collapse whitespace.
    Does NOT remove placeholder tokens (they become plain words after lowercasing).
    Case is ignored: comparison is always case-insensitive.
    """
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
    """
    Same as normalize_strict but also removes whitelisted placeholder tokens.
    """
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
    Clean text for display in the UI (both OCR and Reference columns).
    - Removes emoji, arrows, bullets, BiDi marks
    - Removes brand names (BongaCams)
    - Removes ALL <angle bracket> constructs (CTA buttons, tags)
    - Removes ALL [square bracket] constructs
    - Removes ALL %percent% constructs
    - Collapses extra blank lines
    Preserves original case and punctuation for readability.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _pre_clean(text)
    text = remove_brand_names(text)
    # Remove ALL bracket constructs for display (regardless of whitelist)
    text = _ALL_ANGLE.sub(" ", text)
    text = _ALL_BRACKET.sub(" ", text)
    text = _ALL_PCT.sub(" ", text)
    # Collapse whitespace per line, preserve newlines
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
