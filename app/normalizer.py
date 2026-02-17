"""
Text normalization for OCR vs reference comparison.
"""
import re
import unicodedata

# Placeholder patterns:
#   %anything%        — template variables
#   [identifier]      — subscriber vars like [firstname]
#   <lowercase_var>   — only lowercase/underscore vars like <date>, <firstname>
#                       CTA buttons like <BUY TOKENS> are NOT placeholders
_PLACEHOLDER_PCT     = re.compile(r"%[^%]+%")
_PLACEHOLDER_BRACKET = re.compile(r"\[[^\]]+\]")
_PLACEHOLDER_ANGLE   = re.compile(r"<[a-z_][a-z0-9_ ]*>")   # only lowercase = variables


def _remove_placeholders(text: str) -> str:
    text = _PLACEHOLDER_PCT.sub(" ", text)
    text = _PLACEHOLDER_BRACKET.sub(" ", text)
    text = _PLACEHOLDER_ANGLE.sub(" ", text)
    return text


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _pre_clean(text: str) -> str:
    """
    Pre-clean: remove emoji, arrows, BiDi marks; normalize dashes/spaces/quotes.
    """
    # Emoji & pictographic ranges
    text = re.sub(
        "[\U0001F000-\U0001FFFF\U00002600-\U000027BF\U00002B00-\U00002BFF\uFE00-\uFE0F]",
        "", text
    )
    # All geometric shapes block (U+25B0-U+25FF) — covers ►▸▶▷◆◈◉○●□■▪▫ etc.
    # Dingbat arrows (U+27A0-U+27BF) — covers ➢➤➨ etc.
    # Standard arrows (U+2190-U+21FF)
    # Bullets and middle dot
    text = re.sub(
        r"[\u25B0-\u25FF"   # geometric shapes (includes ▶►▸▷◆ etc.)
        r"\u27A0-\u27BF"    # dingbat arrows
        r"\u2190-\u21FF"    # arrows block
        r"\u2022\u00B7]",   # bullet •, middle dot ·
        "", text
    )
    # BiDi / LTR-RTL embedding marks (very common in Arabic/Hebrew DOCX)
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


def normalize_strict(text: str) -> str:
    """
    Normalize for strict comparison. Lowercase, strip punctuation, collapse whitespace.
    Does NOT remove placeholders.

    Order matters:
    1. Unicode normalize (NFC)
    2. Pre-clean (emoji, symbols, dashes, quotes, BiDi marks)
    3. Collapse spaces (pre_clean may leave extra whitespace)
    4. Lowercase
    5. Remove punctuation (keep only word chars and spaces)
    6. Final whitespace collapse
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _pre_clean(text)
    text = _collapse_whitespace(text)
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = _collapse_whitespace(text)
    return text


def normalize_soft(text: str) -> str:
    """Same as strict but also removes placeholder tokens."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _pre_clean(text)
    text = _collapse_whitespace(text)
    text = text.lower()
    text = _remove_placeholders(text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = _collapse_whitespace(text)
    return text


def clean_for_display(text: str) -> str:
    """
    Clean reference text for display in UI:
    remove emoji, arrows, BiDi marks, placeholders.
    Preserves case and punctuation for readability.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _pre_clean(text)
    text = _remove_placeholders(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def has_placeholder(text: str) -> bool:
    """Return True if text contains any placeholder pattern."""
    return bool(
        _PLACEHOLDER_PCT.search(text)
        or _PLACEHOLDER_BRACKET.search(text)
        or _PLACEHOLDER_ANGLE.search(text)
    )
