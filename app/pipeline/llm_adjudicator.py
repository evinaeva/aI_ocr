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
_DEFAULT_TIMEOUT_S = 10.0

# Decoration brackets stripped from inputs before sending to the LLM. The
# banner OCR never captures `[ X ]` (it's docx-side layout), and stray `(`
# or `)` are typical OCR artefacts at the edges of cropped zones. Marketing
# copy on banners rarely uses parens meaningfully, so dropping them on both
# sides is safe and stops the model from inventing phrase-level diffs.
import re as _re_decor
_DECOR_BRACKETS_RE = _re_decor.compile(r"[\[\]<>()]")

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
    cost_usd: Optional[float] = None        # per-call cost reported by OpenRouter
    real_differences: Optional[list] = None  # [{ocr, ref, kind}, ...]
    ocr_noise: Optional[list] = None         # [{ocr, ref, kind}, ...]
    from_cache: bool = False                 # served from in-session cache

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


def _cache_key(ocr_clean: str, ref_clean: str, lang: str) -> str:
    """Stable key for the in-session LLM cache. Lowercased on both sides
    (per operator rule: capitalisation differences are ignored at this
    stage), so 'Hello' and 'HELLO' hit the same entry."""
    import hashlib
    h = hashlib.sha1()
    h.update(lang.encode("utf-8"))
    h.update(b"\x00")
    h.update(ocr_clean.lower().encode("utf-8"))
    h.update(b"\x00")
    h.update(ref_clean.lower().encode("utf-8"))
    return h.hexdigest()


def adjudicate(
    ocr_text: str,
    expected_text: str,
    lang: str,
    similarity: Optional[float],
    *,
    match_pass: bool,
    cache: Optional[dict] = None,
) -> LLMVerdict:
    """Ask the LLM whether OCR output is semantically equivalent to the expected text.

    Never raises. Failures surface as `called=True, verdict=None, error=<code>`.

    `cache`: optional dict for in-session memoisation by hash(ocr+ref+lang).
    The caller is responsible for the cache's lifetime — typically one dict
    per `_process_session` invocation. Identical (ocr, ref, lang) pairs
    hit cache and don't burn additional API calls.
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
    # We also lowercase both sides per the operator rule "ignore capitalisation
    # differences" — otherwise the model reports `HELLO` vs `hello` as a real
    # diff. Languages without case (Hebrew, Arabic, CJK, etc.) are unaffected.
    from app.normalizer import clean_for_display
    # Beyond `clean_for_display` (which preserves <CTA> / [bracket] for the
    # UI), the LLM should NOT see those decoration brackets at all: the
    # banner OCR will never include them, but they live in the docx, and
    # the model reliably hallucinates a "phrase diff" out of `[ X ]` vs `X`.
    # Strip them on both sides before the prompt.
    _strip_decorations = lambda s: _DECOR_BRACKETS_RE.sub(" ", s)  # noqa: E731
    ocr_clean = _strip_decorations(clean_for_display(ocr_text)).lower()
    ref_clean = _strip_decorations(clean_for_display(expected_text)).lower()

    # In-session cache: identical (ocr, ref, lang) shouldn't burn a second
    # API call. Common when the same banner appears in multiple sessions
    # or the same boilerplate text recurs across zones.
    cache_key = _cache_key(ocr_clean, ref_clean, lang) if cache is not None else None
    if cache is not None and cache_key in cache:
        cached_verdict: LLMVerdict = cache[cache_key]
        # Make a copy with from_cache=True so the caller can see this was free.
        from dataclasses import replace
        return replace(cached_verdict, from_cache=True, latency_ms=0.0)

    body = {
        "model": model,
        "messages": [{"role": "user", "content": _build_prompt(ocr_clean, ref_clean, lang)}],
        "temperature": 0,
        # Bumped from 200 → 600: the structured-diff response (real_differences
        # + ocr_noise arrays) easily blows past 200 tokens on multi-diff
        # responses and gets truncated mid-JSON, causing `bad_response`.
        "max_tokens": 600,
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
    cost_usd = _extract_cost(data)
    if parsed is None:
        return LLMVerdict(called=True, verdict=None, reason=None, error="bad_response",
                          latency_ms=_elapsed_ms(t0), model=model, cost_usd=cost_usd)

    # Fire-and-forget: bump the monthly LLM-judge usage counter. Never
    # block the verdict on a Firestore hiccup.
    try:
        from app.metrics.llm_usage import increment_llm_usage
        increment_llm_usage(cost_usd or 0.0, delta=1)
    except Exception as exc:
        logger.warning("llm_usage_increment_skipped type=%s", type(exc).__name__)

    verdict_obj = LLMVerdict(
        called=True,
        verdict=parsed["verdict"],
        reason=parsed["reason"],
        error=None,
        latency_ms=_elapsed_ms(t0),
        model=model,
        cost_usd=cost_usd,
        real_differences=parsed["real_differences"],
        ocr_noise=parsed["ocr_noise"],
    )

    # Store in the session cache so repeated (ocr, ref, lang) triples
    # don't burn another API call. Cache only successful verdicts.
    if cache is not None and cache_key is not None:
        cache[cache_key] = verdict_obj

    return verdict_obj


def _elapsed_ms(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000.0, 1)


def _build_prompt(ocr_text: str, expected_text: str, lang: str) -> str:
    return (
        "You are a localisation QA assistant. An OCR engine read text from a "
        "marketing image; compare its output against the reference text and "
        "list every difference, classifying each as either a real "
        "localisation issue or an OCR artefact.\n\n"
        "Default to fail. Only return verdict=pass when EVERY difference is "
        "explainable as OCR noise (the operator's rule is: missing a real "
        "issue is worse than flagging a fake one).\n\n"
        "OCR NOISE — list under `ocr_noise`, ignore for the verdict:\n"
        "  - line breaks, whitespace, punctuation spacing, smart/straight quotes\n"
        "  - capitalisation only differences (HELLO vs hello)\n"
        "  - missing/merged diacritics (ț↔ţ, missing umlauts)\n"
        "  - compound-word boundary shifts (same letters, different splits)\n"
        "  - typical OCR substitutions: rn↔m, l↔1, O↔0\n"
        "  - a single stray character of obvious garbage (`ㅎ`, `✪`, `*`)\n"
        "  - brand names, emoji, decoration brackets — already stripped before "
        "you got the inputs, so any symbol-only diff is noise\n\n"
        "REAL DIFFERENCES — list under `real_differences`, drive a fail:\n"
        "  - any number, currency, percent, or date differs (e.g. `13` vs `1`)\n"
        "  - a word changes meaning (different product, action, translation)\n"
        "  - a whole phrase missing on one side or added on the other\n"
        "  - more than one stray character (not a single OCR slip)\n\n"
        f"Language: {lang}\n\n"
        "OCR output:\n<<<\n"
        f"{ocr_text}\n"
        ">>>\n\n"
        "Reference text:\n<<<\n"
        f"{expected_text}\n"
        ">>>\n\n"
        "Respond with ONLY a JSON object — no prose, no code fences. "
        "If a list is empty, return `[]`:\n"
        '{\n'
        '  "verdict": "pass" | "fail",\n'
        '  "reason": "<one short sentence, what tipped the verdict>",\n'
        '  "real_differences": [{"ocr": "<token>", "ref": "<token>", "kind": "number|word|phrase|stray"}],\n'
        '  "ocr_noise": [{"ocr": "<token>", "ref": "<token>", "kind": "whitespace|punct|case|diacritic|compound|substitution|stray"}]\n'
        '}'
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


def _extract_cost(data: dict) -> Optional[float]:
    """Pull per-call USD cost from the OpenRouter response if present.

    OpenRouter reports `usage.cost` as a float in dollars. Robust to a
    missing field or unexpected type — returns None when it can't be parsed.
    """
    try:
        usage = data.get("usage") or {}
        raw = usage.get("cost")
        if raw is None:
            return None
        cost = float(raw)
        return cost if cost >= 0 else None
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_response(data: dict) -> Optional[dict]:
    """Parse the OpenRouter response. Returns a dict with the structured
    diff or None when the response can't be interpreted.

    The verdict in the parsed dict is **derived from `real_differences`**,
    not from whatever string the LLM put in `verdict` — this enforces the
    "default fail" rule even when the model returns inconsistent fields.
    """
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str):
        return None
    obj = _extract_json_object(content)
    if obj is None:
        return None

    # Always coerce diff lists to safe types so downstream code can rely
    # on them.
    def _coerce_diffs(value) -> list:
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            if not isinstance(item, dict):
                continue
            out.append({
                "ocr": str(item.get("ocr", ""))[:120],
                "ref": str(item.get("ref", ""))[:120],
                "kind": str(item.get("kind", ""))[:30],
            })
        return out

    real = _coerce_diffs(obj.get("real_differences"))
    noise = _coerce_diffs(obj.get("ocr_noise"))
    reason = str(obj.get("reason") or "")[:300]

    # Authoritative verdict from the diff list, NOT from the model's
    # `verdict` field — protects against the model contradicting itself
    # (e.g. listing a real diff but saying verdict=pass).
    verdict = "fail" if real else "pass"

    return {
        "verdict": verdict,
        "reason": reason,
        "real_differences": real,
        "ocr_noise": noise,
    }
