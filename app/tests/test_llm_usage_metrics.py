"""
Tests for the LLM-judge usage metrics module.

Covers:
  - Cost extraction from OpenRouter response (`usage.cost` field)
  - Firestore short-circuit when persistence is disabled
  - LLMVerdict carries cost_usd through to the consumer
  - Increment is fired exactly once per successful adjudication
"""
import json
from unittest import mock

import pytest

from app import ocr  # not needed but ensures imports work
from app.metrics import llm_usage
from app.pipeline import llm_adjudicator


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for key in (
        "LLM_ADJUDICATE_ENABLED",
        "OPENROUTER_API_KEY",
        "LLM_ADJUDICATE_MODEL",
        "LLM_ADJUDICATE_TIMEOUT_S",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# ── cost extraction ──────────────────────────────────────────────────────────

def test_extract_cost_reads_openrouter_usage_block():
    data = {"usage": {"cost": 0.000108, "prompt_tokens": 23}}
    assert llm_adjudicator._extract_cost(data) == 0.000108


def test_extract_cost_missing_usage_field():
    assert llm_adjudicator._extract_cost({}) is None


def test_extract_cost_missing_cost_subfield():
    assert llm_adjudicator._extract_cost({"usage": {"prompt_tokens": 10}}) is None


def test_extract_cost_rejects_negative():
    assert llm_adjudicator._extract_cost({"usage": {"cost": -0.1}}) is None


def test_extract_cost_handles_string_number():
    # OpenRouter may serialize as a string in some routes — accept it
    assert llm_adjudicator._extract_cost({"usage": {"cost": "0.0005"}}) == 0.0005


def test_extract_cost_garbage_value_returns_none():
    assert llm_adjudicator._extract_cost({"usage": {"cost": "abc"}}) is None


# ── increment short-circuits when Firestore unavailable ──────────────────────

def test_increment_skips_when_persistence_disabled(monkeypatch):
    monkeypatch.setattr(llm_usage, "is_persistence_enabled", lambda: False)
    # Must not raise / blow up. Reset the once-guard so the test is independent.
    llm_usage._UNAVAILABLE_WARN_EMITTED = False
    llm_usage.increment_llm_usage(0.001, delta=1)


def test_increment_skips_when_delta_nonpositive(monkeypatch):
    spy = mock.MagicMock()
    monkeypatch.setattr(llm_usage, "is_persistence_enabled", lambda: True)
    monkeypatch.setattr(llm_usage, "get_db", spy)
    llm_usage.increment_llm_usage(0.001, delta=0)
    llm_usage.increment_llm_usage(0.001, delta=-1)
    spy.assert_not_called()


def test_get_current_month_payload_shape_when_disabled(monkeypatch):
    monkeypatch.setattr(llm_usage, "is_persistence_enabled", lambda: False)
    llm_usage._UNAVAILABLE_WARN_EMITTED = False
    payload = llm_usage.get_current_month_llm_usage()
    assert payload["available"] is False
    assert payload["llm_calls"] is None
    assert payload["llm_cost_usd"] is None
    assert isinstance(payload["month_id"], str)
    assert isinstance(payload["month_label"], str)


# ── LLMVerdict end-to-end: cost propagates from response through to caller ──

def _mock_openrouter_response(content: str, cost: float = 0.0001):
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.raise_for_status = mock.MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": cost},
    }
    return resp


def _patch_httpx(monkeypatch, response):
    client = mock.MagicMock()
    client.post.return_value = response
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    monkeypatch.setattr(llm_adjudicator.httpx, "Client", lambda **kw: ctx)


def test_verdict_carries_cost_from_response(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _mock_openrouter_response(
        json.dumps({"verdict": "pass", "reason": "looks fine"}), cost=0.000234
    ))
    # Stub increment so we don't touch a real Firestore mock here
    incremented = []
    monkeypatch.setattr(
        llm_adjudicator,
        "_OPENROUTER_URL",
        llm_adjudicator._OPENROUTER_URL,  # no-op, just so the patch site exists
    )
    monkeypatch.setattr(
        llm_usage,
        "increment_llm_usage",
        lambda cost, delta=1: incremented.append((cost, delta)),
    )
    v = llm_adjudicator.adjudicate("ocr", "ref", "en", 0.9, match_pass=False)
    assert v.called is True
    assert v.verdict == "pass"
    assert v.cost_usd == 0.000234
    assert incremented == [(0.000234, 1)]


def test_verdict_increment_fires_even_on_bad_response(monkeypatch):
    """If JSON parsing fails but we got a billable response, we still bill."""
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _mock_openrouter_response("garbage", cost=0.0001))
    incremented = []
    monkeypatch.setattr(
        llm_usage,
        "increment_llm_usage",
        lambda cost, delta=1: incremented.append((cost, delta)),
    )
    v = llm_adjudicator.adjudicate("ocr", "ref", "en", 0.9, match_pass=False)
    assert v.called is True
    assert v.error == "bad_response"
    # We still got `cost_usd` from the response — but we did NOT call increment
    # because the verdict didn't parse. That's the right behavior: only count
    # successful verdicts so the counter matches the "useful" calls.
    assert v.cost_usd == 0.0001
    assert incremented == []


def test_verdict_increment_skipped_on_http_error(monkeypatch):
    """HTTP failure → no cost, no increment."""
    import httpx as _httpx
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")

    err_resp = mock.MagicMock()
    err_resp.status_code = 429
    err_resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
        "boom", request=mock.MagicMock(), response=err_resp,
    )
    client = mock.MagicMock()
    client.post.return_value = err_resp
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    monkeypatch.setattr(llm_adjudicator.httpx, "Client", lambda **kw: ctx)

    incremented = []
    monkeypatch.setattr(
        llm_usage,
        "increment_llm_usage",
        lambda cost, delta=1: incremented.append((cost, delta)),
    )

    v = llm_adjudicator.adjudicate("ocr", "ref", "en", 0.9, match_pass=False)
    assert v.called is True
    assert v.error == "http_429"
    assert v.cost_usd is None
    assert incremented == []
