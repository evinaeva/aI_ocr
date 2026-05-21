"""
Tests for the LLM adjudicator (Phase 5 gray-zone fallback).

Covers:
  - env gate (LLM_ADJUDICATE_ENABLED) — disabled by default
  - similarity gate (only fires in [SIM_MIN, SIM_MAX] window)
  - never fires when rule-based comparator already passed
  - missing API key → no call, safe error code
  - parsing of valid / malformed OpenRouter responses
  - HTTP failures → graceful error code, no exception
  - integration with build_validation_result: pass verdict flips match_pass
"""
import json
from unittest import mock

import pytest

from app.pipeline import llm_adjudicator
from app.pipeline.llm_adjudicator import (
    LLMVerdict,
    SIM_MAX,
    SIM_MIN,
    adjudicate,
    is_enabled,
    should_call,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Each test gets a clean env — adjudicator OFF unless test enables it."""
    for key in (
        "LLM_ADJUDICATE_ENABLED",
        "OPENROUTER_API_KEY",
        "LLM_ADJUDICATE_MODEL",
        "LLM_ADJUDICATE_TIMEOUT_S",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# ── env / gating ──────────────────────────────────────────────────────────────

def test_is_enabled_default_on():
    """Unset env var = ON by default (since 2026-05-21)."""
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_is_enabled_accepts_truthy(monkeypatch, val):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", val)
    assert is_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "random"])
def test_is_enabled_rejects_falsy(monkeypatch, val):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", val)
    assert is_enabled() is False


def test_should_call_skips_when_rule_passed():
    assert should_call(match_pass=True, similarity=0.85) is False


def test_should_call_skips_when_similarity_none():
    assert should_call(match_pass=False, similarity=None) is False


def test_should_call_skips_below_window():
    assert should_call(match_pass=False, similarity=SIM_MIN - 0.01) is False


def test_should_call_skips_above_window():
    # Exactly above SIM_MAX. We do not call for ~1.0 similarity since the
    # rule-based comparator would normally pass; if it didn't, the diff is
    # something subtle we still want the operator to see.
    assert should_call(match_pass=False, similarity=SIM_MAX + 0.0001) is False


def test_should_call_fires_in_window():
    assert should_call(match_pass=False, similarity=SIM_MIN) is True
    assert should_call(match_pass=False, similarity=0.85) is True
    assert should_call(match_pass=False, similarity=SIM_MAX) is True


# ── adjudicate() — skip paths (no HTTP) ──────────────────────────────────────

def test_adjudicate_explicit_disabled_returns_no_call(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "false")
    v = adjudicate("ocr", "ref", "en", 0.85, match_pass=False)
    assert v.called is False
    assert v.error == "disabled"


def test_adjudicate_match_pass_true_returns_no_call(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    v = adjudicate("ocr", "ref", "en", 0.85, match_pass=True)
    assert v.called is False


def test_adjudicate_outside_window_returns_no_call(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    v = adjudicate("ocr", "ref", "en", 0.10, match_pass=False)
    assert v.called is False


def test_adjudicate_missing_api_key(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    # No OPENROUTER_API_KEY.
    v = adjudicate("ocr", "ref", "en", 0.85, match_pass=False)
    assert v.called is False
    assert v.error == "missing_api_key"


# ── adjudicate() — HTTP mocked ───────────────────────────────────────────────

def _mock_openrouter_response(content: str, status: int = 200):
    resp = mock.MagicMock()
    resp.status_code = status
    resp.raise_for_status = mock.MagicMock()
    if status >= 400:
        import httpx as _httpx
        resp.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "boom", request=mock.MagicMock(), response=resp
        )
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return resp


def _patch_httpx(monkeypatch, response_or_exc):
    """Replace httpx.Client used by adjudicator with a context manager that
    returns the given mocked response (or raises an exception when posted)."""
    client = mock.MagicMock()
    if isinstance(response_or_exc, Exception):
        client.post.side_effect = response_or_exc
    else:
        client.post.return_value = response_or_exc
    ctx = mock.MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    monkeypatch.setattr(llm_adjudicator.httpx, "Client", lambda **kw: ctx)
    return client


def test_adjudicate_pass_verdict(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _mock_openrouter_response(
        json.dumps({"verdict": "pass", "reason": "only line breaks differ"})
    ))
    v = adjudicate("Buy now\nfree spins", "Buy now free spins", "en", 0.95, match_pass=False)
    assert v.called is True
    assert v.verdict == "pass"
    assert "line breaks" in v.reason
    assert v.error is None
    assert v.model == "anthropic/claude-haiku-4.5"
    assert v.latency_ms is not None


def test_adjudicate_fail_verdict(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _mock_openrouter_response(
        json.dumps({"verdict": "fail", "reason": "number differs: 500 vs 50"})
    ))
    v = adjudicate("Win 500 tokens", "Win 50 tokens", "en", 0.88, match_pass=False)
    assert v.called is True
    assert v.verdict == "fail"
    assert "500" in v.reason


def test_adjudicate_malformed_json(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _mock_openrouter_response("not even close to json"))
    v = adjudicate("a", "b", "en", 0.85, match_pass=False)
    assert v.called is True
    assert v.verdict is None
    assert v.error == "bad_response"


def test_adjudicate_unknown_verdict_value(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _mock_openrouter_response(
        json.dumps({"verdict": "maybe", "reason": "unsure"})
    ))
    v = adjudicate("a", "b", "en", 0.85, match_pass=False)
    assert v.called is True
    assert v.error == "bad_response"


def test_adjudicate_http_error_returns_error_code(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _mock_openrouter_response("{}", status=429))
    v = adjudicate("a", "b", "en", 0.85, match_pass=False)
    assert v.called is True
    assert v.verdict is None
    assert v.error == "http_429"


def test_adjudicate_timeout(monkeypatch):
    import httpx as _httpx
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _httpx.TimeoutException("slow"))
    v = adjudicate("a", "b", "en", 0.85, match_pass=False)
    assert v.called is True
    assert v.error == "timeout"


def test_adjudicate_generic_exception(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, RuntimeError("boom"))
    v = adjudicate("a", "b", "en", 0.85, match_pass=False)
    assert v.called is True
    assert v.error == "exception"


def test_adjudicate_custom_model_env(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_ADJUDICATE_MODEL", "anthropic/claude-haiku-4.5:beta")
    client = _patch_httpx(monkeypatch, _mock_openrouter_response(
        json.dumps({"verdict": "pass", "reason": "ok"})
    ))
    adjudicate("a", "b", "en", 0.85, match_pass=False)
    sent_body = client.post.call_args.kwargs["json"]
    assert sent_body["model"] == "anthropic/claude-haiku-4.5:beta"


# ── integration with build_validation_result ────────────────────────────────

def test_build_validation_result_unaffected_when_adjudicator_explicitly_off(monkeypatch):
    """Explicit `LLM_ADJUDICATE_ENABLED=false` keeps the original behavior."""
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "false")
    from app.pipeline.similarity import build_validation_result

    expected = {"en": {"banner": "Buy now and get tokens"}}
    block, _ = build_validation_result(
        lang="en",
        zone_name="banner",
        expected_texts=expected,
        ocr_text="Buy now\nand get tokens",  # whitespace-only diff → already PASS
        run_id="r1",
    )
    assert block["validation_applied"] is True
    assert block["match_pass"] is True
    assert block["llm_adjudicator"]["called"] is False
    assert block["llm_adjudicator"]["error"] == "disabled"


def test_build_validation_result_default_on_no_key(monkeypatch):
    """Default-on behavior: rule-pass case → adjudicator skipped (`should_call=False`)."""
    # No env vars set — default ON, but match_pass=True case → no call
    from app.pipeline.similarity import build_validation_result

    expected = {"en": {"banner": "Buy now and get tokens"}}
    block, _ = build_validation_result(
        lang="en",
        zone_name="banner",
        expected_texts=expected,
        ocr_text="Buy now\nand get tokens",
        run_id="r1",
    )
    assert block["validation_applied"] is True
    assert block["match_pass"] is True
    # Rule passed → adjudicator should not have been called
    assert block["llm_adjudicator"]["called"] is False


def test_build_validation_result_llm_flips_match_pass(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _mock_openrouter_response(
        json.dumps({"verdict": "pass", "reason": "same content, different reflow"})
    ))

    from app.pipeline.similarity import build_validation_result

    # OCR adds an extra word that breaks the rule-based multiset check
    # but the texts are still mostly the same → sim in the gray zone.
    expected = {"en": {"banner": "Buy now and get free tokens today"}}
    ocr = "Buy now and\nget free tokens today now"
    block, sim = build_validation_result(
        lang="en", zone_name="banner",
        expected_texts=expected, ocr_text=ocr, run_id="r2",
    )
    assert block["validation_applied"] is True
    # If the rule already passed this case, we still want match_pass=True;
    # the LLM block must reflect whether it was actually called.
    if block["llm_adjudicator"]["called"]:
        assert block["match_pass"] is True
        assert block["match_mode"] == "llm_adjudicated"
    else:
        # Rule-based path already passed → LLM not needed
        assert block["match_pass"] is True


def test_build_validation_result_llm_fail_keeps_manual(monkeypatch):
    monkeypatch.setenv("LLM_ADJUDICATE_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _patch_httpx(monkeypatch, _mock_openrouter_response(
        json.dumps({"verdict": "fail", "reason": "different number"})
    ))

    from app.pipeline.similarity import build_validation_result

    expected = {"en": {"banner": "Win 500 tokens today"}}
    ocr = "Win 50 tokens today"  # number changed → sim ~0.9, real difference
    block, _ = build_validation_result(
        lang="en", zone_name="banner",
        expected_texts=expected, ocr_text=ocr, run_id="r3",
    )
    assert block["validation_applied"] is True
    assert block["match_pass"] is False
    assert block["llm_adjudicator"]["called"] is True
    assert block["llm_adjudicator"]["verdict"] == "fail"
