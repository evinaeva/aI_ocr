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


def normalize_strict(text: str) -> str:
    """
    Remove extra whitespace, lowercase, strip punctuation.
    Does NOT remove placeholders.
    """
    if not text:
        return ""
    # Unicode normalize
    text = unicodedata.normalize("NFC", text)
    # Lowercase
    text = text.lower()
    # Remove punctuation (keep letters, digits, whitespace)
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
