"""
Text normalization for OCR vs reference comparison.
"""
import re
import unicodedata

# Placeholder patterns: %anything%, [identifier]
_PLACEHOLDER_PCT = re.compile(r"%[^%]+%")
_PLACEHOLDER_BRACKET = re.compile(r"\[[^\]]+\]")
_PLACEHOLDER_ANGLE = re.compile(r"<[^>]+>")


def _remove_placeholders(text: str) -> str:
    text = _PLACEHOLDER_PCT.sub(" ", text)
    text = _PLACEHOLDER_BRACKET.sub(" ", text)
    text = _PLACEHOLDER_ANGLE.sub(" ", text)
    return text


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _pre_clean(text: str) -> str:
    """
    Pre-clean before main normalization:
    - Replace non-breaking spaces (\xa0, \u202f, \u2009 etc.) with regular space
    - Replace em-dash, en-dash, figure dash with hyphen
    - Replace smart/curly quotes with straight quotes
    - Replace soft hyphen (invisible) with nothing
    - Replace ellipsis character with three dots
    """
    # Non-breaking and other unicode spaces → regular space
    text = re.sub(r"[\xa0\u00a0\u202f\u2009\u2007\u2008\u200a\u3000\u1680\u180e]", " ", text)
    # Em-dash, en-dash, figure dash, horizontal bar → hyphen
    text = re.sub(r"[\u2013\u2014\u2015\u2012\u2011]", "-", text)
    # Smart quotes → straight quotes
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u00ab", '"').replace("\u00bb", '"')
    text = text.replace("\u2039", "'").replace("\u203a", "'")
    # Soft hyphen (invisible character) → nothing
    text = text.replace("\u00ad", "")
    # Ellipsis character → three dots
    text = text.replace("\u2026", "...")
    # Zero-width characters → nothing
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    return text


def normalize_strict(text: str) -> str:
    """
    Remove extra whitespace, lowercase, strip punctuation.
    Also normalises unicode dashes/spaces/quotes so OCR text can match reference.
    Does NOT remove placeholders.
    """
    if not text:
        return ""
    # Unicode NFC
    text = unicodedata.normalize("NFC", text)
    # Pre-clean unicode variants before lowercasing / punct removal
    text = _pre_clean(text)
    # Lowercase
    text = text.lower()
    # Remove punctuation (keep letters, digits, whitespace, underscore)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    # Collapse whitespace
    text = _collapse_whitespace(text)
    return text


def normalize_soft(text: str) -> str:
    """
    Same as strict but also removes placeholder tokens %text%, [text], <text>.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _pre_clean(text)
    text = text.lower()
    text = _remove_placeholders(text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = _collapse_whitespace(text)
    return text


def has_placeholder(text: str) -> bool:
    """Return True if text contains any placeholder pattern."""
    return bool(
        _PLACEHOLDER_PCT.search(text)
        or _PLACEHOLDER_BRACKET.search(text)
        or _PLACEHOLDER_ANGLE.search(text)
    )
