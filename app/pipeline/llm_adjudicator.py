"""
LLM adjudicator for OCR-vs-expected comparison.

Fires only when `compare_lines` returns pass=False AND the Levenshtein
similarity is inside the [SIM_MIN, SIM_MAX] gray zone — the cases where
the rule-based comparator likely produced a false MANUAL due to line
break / whitespace / minor OCR artefacts.

Disabled by default. Enable with env `LLM_ADJUDICATE_ENABLED=true` and
set `OPENROUTER_API_KEY`.

Model is hard-pinned to Claude Haiku 4.5 via OpenRouter. There is no
escalation to a larger model on uncertain verdicts — return uncertainty
as-is.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODEL = "anthropic/claude-haiku-4.5"
_DEFAULT_TIMEOUT_S = 5.0

# Gray zone — outside this range we trust the rule-based result and skip the call.
SIM_MIN = 0.75
SIM_MAX = 0.99


@dataclass
class LLMVerdict:
    called: bool
    verdict: Optional[str]      # "pass" | "fail" | None when not called or error
    reason: Optional[str]
    error: Optional[str]        # short error code if call failed
    latency_ms: Optional[float]
    model: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def is_enabled() -> bool:
    """LLM judge is ON by default. Explicit `LLM_ADJUDICATE_ENABLED=false` disables it.

    Without an OPENROUTER_API_KEY the call is still skipped gracefully
    (see `adjudicate()`), so flipping the default to ON is safe even in
    environments that haven't configured the key yet.
    """
    val = os.getenv("LLM_ADJUDICATE_ENABLED", "").strip().lower()
    if val == "":
        return True
    return val in ("1", "true", "yes", "on")


def should_call(match_pass: bool, similarity: Optional[float]) -> bool:
    """Skip the LLM unless the rule said FAIL and similarity is in the gray zone."""
    if match_pass:
        return False
    if similarity is None:
        return False
    return SIM_MIN <= float(similarity) <= SIM_MAX


def adjudicate(
    ocr_text: str,
    expected_text: str,
    lang: str,
    similarity: Optional[float],
    *,
    match_pass: bool,
) -> LLMVerdict:
    """Ask the LLM whether OCR output is semantically equivalent to the expected text.

    Never raises. Failures surface as `called=True, verdict=None, error=<code>`.
    """
    if not is_enabled():
        return LLMVerdict(called=False, verdict=None, reason=None,
                          error="disabled", latency_ms=None)

    if not should_call(match_pass, similarity):
        return LLMVerdict(called=False, verdict=None, reason=None,
                          error=None, latency_ms=None)

    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return LLMVerdict(called=False, verdict=None, reason=None,
                          error="missing_api_key", latency_ms=None)

    model = (os.getenv("LLM_ADJUDICATE_MODEL", "").strip() or _DEFAULT_MODEL)
    try:
        timeout_s = float(os.getenv("LLM_ADJUDICATE_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
    except ValueError:
        timeout_s = _DEFAULT_TIMEOUT_S

    # Strip emojis, brand names, BiDi marks etc. on BOTH sides before sending
    # to the LLM — otherwise the model invents differences like "❤ vs ⚽" that
    # are not real localisation errors. `clean_for_display` is the canonical
    # display-time cleanup; using it here keeps both inputs apples-to-apples.
    from app.normalizer import clean_for_display
    ocr_clean = clean_for_display(ocr_text)
    ref_clean = clean_for_display(expected_text)

    body = {
        "model": model,
        "messages": [{"role": "user", "content": _build_prompt(ocr_clean, ref_clean, lang)}],
        "temperature": 0,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/evinaeva/aI_ocr",
        "X-Title": "aI_ocr-adjudicator",
    }

    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(_OPENROUTER_URL, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        return LLMVerdict(called=True, verdict=None, reason=None, error="timeout",
                          latency_ms=_elapsed_ms(t0), model=model)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response is not None else 0
        return LLMVerdict(called=True, verdict=None, reason=None,
                          error=f"http_{status}", latency_ms=_elapsed_ms(t0), model=model)
    except Exception as e:
        logger.warning("llm_adjudicate_error type=%s", type(e).__name__)
        return LLMVerdict(called=True, verdict=None, reason=None, error="exception",
                          latency_ms=_elapsed_ms(t0), model=model)

    parsed = _parse_response(data)
    if parsed is None:
        return LLMVerdict(called=True, verdict=None, reason=None, error="bad_response",
                          latency_ms=_elapsed_ms(t0), model=model)
    verdict, reason = parsed
    return LLMVerdict(called=True, verdict=verdict, reason=reason, error=None,
                      latency_ms=_elapsed_ms(t0), model=model)


def _elapsed_ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000.0, 1)


def _build_prompt(ocr_text: str, expected_text: str, lang: str) -> str:
    return (
        "You are a localization QA assistant. An OCR engine read text from a "
        "marketing/UI image; compare its output against the reference text and "
        "decide if they are semantically equivalent.\n\n"
        "Treat as equivalent — IGNORE these differences:\n"
        "  - line breaks and whitespace (any reflow is fine)\n"
        "  - punctuation and capitalization (commas, exclamation marks, "
        "spaces around punctuation, smart vs straight quotes)\n"
        "  - missing or merged diacritics (e.g. `ț` vs `ţ`, missing umlauts)\n"
        "  - words split into pieces or merged together (compound-word "
        "boundary differences; both sides have the same letters in order)\n"
        "  - obvious OCR artefacts: rn↔m, l↔1, O↔0, broken hyphenation,\n"
        "    a single dropped or added stray character (e.g. `ㅎ`, `✪`, `*`)\n"
        "  - emoji, brand names, decoration brackets — these were already\n"
        "    stripped from both inputs, so any remaining symbol-only diff\n"
        "    is NOT a real localisation error\n\n"
        "Treat as DIFFERENT — return fail when:\n"
        "  - any content word changes meaning (different product, action,\n"
        "    different translation)\n"
        "  - a number, currency, percent, or date is different (e.g. `13` vs `1`)\n"
        "  - a whole phrase is missing from one side or added on the other\n\n"
        f"Language: {lang}\n\n"
        "OCR output:\n<<<\n"
        f"{ocr_text}\n"
        ">>>\n\n"
        "Reference text:\n<<<\n"
        f"{expected_text}\n"
        ">>>\n\n"
        "Respond with ONLY a JSON object — no prose, no code fences:\n"
        '{"verdict": "pass" | "fail", "reason": "<one short sentence>"}'
    )


import re as _re

# Some providers (Bedrock-routed Anthropic on OpenRouter) ignore the
# `response_format=json_object` hint and wrap the JSON in ```json ... ```
# fences. Strip those before parsing. As a last resort we extract the
# first {...} block from the content.
_FENCE_RE = _re.compile(r"^```(?:json)?\s*|\s*```$", _re.IGNORECASE | _re.MULTILINE)
_BRACE_BLOCK_RE = _re.compile(r"\{.*\}", _re.DOTALL)


def _extract_json_object(content: str) -> Optional[dict]:
    """Tolerate code fences and surrounding prose."""
    candidates = [content, _FENCE_RE.sub("", content).strip()]
    m = _BRACE_BLOCK_RE.search(content)
    if m:
        candidates.append(m.group(0))
    for s in candidates:
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _parse_response(data: dict) -> Optional[Tuple[str, str]]:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    obj = _extract_json_object(content)
    if obj is None:
        return None
    verdict = obj.get("verdict")
    reason = obj.get("reason") or ""
    if verdict not in ("pass", "fail"):
        return None
    return verdict, str(reason)[:300]
