"""
Tests for Azure retry-once and OCR.Space garbage filter.

Both surfaced from operator feedback (2026-05) on the 1.zip archive:
  - Azure intermittently returned no response on small crops, leaving
    consensus with too few votes.
  - OCR.Space sometimes returned punctuation-only strings (long runs of
    `#`) that wonby-length the consensus tiebreaker and poisoned the
    downstream comparison.
"""
from unittest import mock

import httpx
import pytest

from app import ocr


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("AZURE_OCR_ENDPOINT", "https://azure.test")
    monkeypatch.setenv("AZURE_OCR_KEY", "key")
    monkeypatch.setenv("OCR_SPACE_API_KEY", "ocrkey")
    yield


def _azure_response(blocks):
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.raise_for_status = mock.MagicMock()
    resp.json.return_value = {"readResult": {"blocks": blocks}}
    return resp


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


def _patch_httpx_post(monkeypatch, responses):
    """Replace `httpx.Client` so successive calls return successive responses."""
    if not isinstance(responses, list):
        responses = [responses]
    iterator = iter(responses)

    def fake_post(*_args, **_kwargs):
        nxt = next(iterator)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    client = mock.MagicMock()
    client.post.side_effect = fake_post
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    monkeypatch.setattr(ocr.httpx, "Client", lambda **kw: ctx)
    return client


# ── Azure retry-once ─────────────────────────────────────────────────────────

def test_azure_succeeds_on_first_call(monkeypatch):
    response = _azure_response([{"lines": [{"text": "Hello world", "words": [{"confidence": 0.9}]}]}])
    client = _patch_httpx_post(monkeypatch, response)

    result = ocr._ocr_azure(b"img")

    assert result == ("Hello world", 0.9)
    assert client.post.call_count == 1


def test_azure_retries_when_first_response_empty(monkeypatch):
    empty = _azure_response([])  # no blocks → None
    success = _azure_response([{"lines": [{"text": "Got it", "words": []}]}])
    client = _patch_httpx_post(monkeypatch, [empty, success])

    result = ocr._ocr_azure(b"img")

    assert result == ("Got it", None)
    assert client.post.call_count == 2


def test_azure_retries_after_exception(monkeypatch):
    success = _azure_response([{"lines": [{"text": "Recovered", "words": []}]}])
    client = _patch_httpx_post(monkeypatch, [httpx.TimeoutException("slow"), success])

    result = ocr._ocr_azure(b"img")

    assert result == ("Recovered", None)
    assert client.post.call_count == 2


def test_azure_returns_none_after_max_attempts(monkeypatch):
    empty1 = _azure_response([])
    empty2 = _azure_response([])
    client = _patch_httpx_post(monkeypatch, [empty1, empty2])

    result = ocr._ocr_azure(b"img")

    assert result is None
    assert client.post.call_count == ocr.AZURE_MAX_ATTEMPTS == 2


def test_azure_skipped_without_env(monkeypatch):
    monkeypatch.delenv("AZURE_OCR_ENDPOINT", raising=False)
    result = ocr._ocr_azure(b"img")
    assert result is None


# ── OCR.Space garbage filter ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "garbage_text",
    [
        "###########",
        "....",
        "## # ##",
        ".,!?",
        "-- - ---",
    ],
)
def test_ocrspace_punctuation_only_treated_as_no_response(monkeypatch, garbage_text):
    """OCR.Space sometimes returns only `#` etc. on tight crops; must be filtered."""
    response = _ocrspace_response(garbage_text)
    _patch_httpx_post(monkeypatch, [response, response, response])  # 3 retries

    result = ocr._ocr_ocrspace(b"img")

    assert result is None  # filtered as garbage


@pytest.mark.parametrize(
    "valid_text",
    [
        "Hello",
        "13 июня",
        "5",   # bare digit still has alnum
        "A.",
        "# 5 tokens",  # mixed garbage+content → kept
    ],
)
def test_ocrspace_text_with_letters_or_digits_passes(monkeypatch, valid_text):
    response = _ocrspace_response(valid_text)
    _patch_httpx_post(monkeypatch, response)

    result = ocr._ocr_ocrspace(b"img")

    assert result == (valid_text, None)


def test_ocrspace_garbage_retries_then_recovers(monkeypatch):
    """Punctuation-only response triggers retry; real text on later attempt wins."""
    garbage = _ocrspace_response("###")
    real = _ocrspace_response("GIFT")
    _patch_httpx_post(monkeypatch, [garbage, real])

    result = ocr._ocr_ocrspace(b"img")

    assert result == ("GIFT", None)
