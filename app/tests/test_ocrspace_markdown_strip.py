"""
Tests that OCR.Space's markdown-header prefixes get stripped by the
same shared helper that handles Azure's.

Operator screenshot 2026-05 (after PR #82 deploy): the column with
`# DARČEKOVÁ KOLEKCIA / ## PRE MAJSTROVSTVÁ SVETA!` was OCR.Space,
not Azure. PR #82 fixed Azure; this PR extends the same strip to
OCR.Space.
"""
from unittest import mock

import pytest

from app import ocr


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("OCR_SPACE_API_KEY", "ocrkey")
    yield


def _ocrspace_response(text: str, exit_code: int = 1):
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.raise_for_status = mock.MagicMock()
    resp.json.return_value = {
        "IsErroredOnProcessing": False,
        "OCRExitCode": exit_code,
        "ParsedResults": [{"ParsedText": text}],
    }
    return resp


def _patch_httpx_post(monkeypatch, response):
    client = mock.MagicMock()
    client.post.return_value = response
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    monkeypatch.setattr(ocr.httpx, "Client", lambda **kw: ctx)


# ── Shared markdown stripper ─────────────────────────────────────────────────

def test_strip_markdown_headers_helper_exists():
    """The helper is renamed in this PR; old name is a backwards-compat alias."""
    assert callable(ocr._strip_markdown_headers)
    assert ocr._strip_markdown_headers is ocr._strip_azure_markdown


# ── OCR.Space response cleanup ───────────────────────────────────────────────

def test_ocrspace_strips_leading_hashes_per_line(monkeypatch):
    raw = "# DARČEKOVÁ\n## KOLEKCIA\n### PRE MAJSTROVSTVÁ\n#### SVETA!"
    _patch_httpx_post(monkeypatch, _ocrspace_response(raw))

    result = ocr._ocr_ocrspace(b"img")

    assert result is not None
    text, _ = result
    expected = "DARČEKOVÁ\nKOLEKCIA\nPRE MAJSTROVSTVÁ\nSVETA!"
    assert text == expected


def test_ocrspace_preserves_inline_hash(monkeypatch):
    """`#hashtag` mid-line is content, not a header marker."""
    raw = "Buy with C# code\nUse #promo at checkout"
    _patch_httpx_post(monkeypatch, _ocrspace_response(raw))

    result = ocr._ocr_ocrspace(b"img")
    assert result is not None
    assert "C#" in result[0]
    assert "#promo" in result[0]


def test_ocrspace_clean_text_passes_through(monkeypatch):
    raw = "GIFT COLLECTION\nFOR THE WORLD\nCHAMPIONSHIP!"
    _patch_httpx_post(monkeypatch, _ocrspace_response(raw))

    result = ocr._ocr_ocrspace(b"img")
    assert result == (raw, None)


def test_ocrspace_punctuation_only_still_filtered_after_strip(monkeypatch):
    """The punctuation-only filter (PR #79) still runs after the markdown
    strip. `# ## ###` (after strip: empty whitespace) should be rejected."""
    raw = "# \n## \n### "
    _patch_httpx_post(monkeypatch, _ocrspace_response(raw))
    _patch_httpx_post(monkeypatch, _ocrspace_response(raw))
    _patch_httpx_post(monkeypatch, _ocrspace_response(raw))

    result = ocr._ocr_ocrspace(b"img")
    # After strip → mostly whitespace. After whitespace strip in _ocr_ocrspace_once
    # → empty text. Returns None.
    assert result is None
