"""
Phase 4: Deterministic Multi-Engine Consensus.

resolve_consensus(engine_results, engines_configured) -> dict

Fully deterministic. No similarity. No persistence.

Priority order for zone_status:
  1. no_engines_configured
  2. all_engines_failed
  3. no_consensus   (rule_used == no_confidence_fallback)
  4. low_confidence (confidence < 0.70)
  5. OK
"""
from __future__ import annotations

import re
from typing import List, Optional

from .ocr_dispatcher import ZoneEngineResult

# Regex: remove ASCII punctuation only.
# Backtick, %, <, >, [, ] are NOT removed.
_PUNCT_RE = re.compile(r'[!"#$&\'()*+,./:;=?@^_{}|~-]')


def normalize_for_consensus(text: str) -> str:
    """
    Steps:
    1. Lowercase (Unicode-aware)
    2. Collapse whitespace (\\s+ -> single space)
    3. Remove ASCII punctuation only (see _PUNCT_RE)
    4. Strip leading/trailing whitespace
    """
    t = text.lower()
    t = re.sub(r'\s+', ' ', t)
    t = _PUNCT_RE.sub('', t)
    return t.strip()


def _valid_confidence(confidence) -> Optional[float]:
    """Return confidence if 0.0 <= conf <= 1.0, else None."""
    if confidence is None:
        return None
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return None
    if 0.0 <= c <= 1.0:
        return c
    return None


def resolve_consensus(
    engine_results: List[ZoneEngineResult],
    engines_configured: bool,
) -> dict:
    """
    Resolve consensus from engine results.

    Args:
        engine_results: list of ZoneEngineResult (may be empty).
        engines_configured: False when zone.engines == [].

    Returns:
        dict with keys:
            selected_engine, selected_text, rule_used,
            zone_status, reason
    """
    # ── Case A: No engines configured ────────────────────────────────────────
    if not engines_configured:
        return {
            "selected_engine": None,
            "selected_text": None,
            "rule_used": None,
            "zone_status": "MANUAL",
            "reason": "no_engines_configured",
        }

    # ── Filter valid engines (error is None) ──────────────────────────────────
    valid: List[ZoneEngineResult] = [
        r for r in engine_results if r.error is None
    ]

    # ── Case B: All engines failed ────────────────────────────────────────────
    if not valid:
        return {
            "selected_engine": None,
            "selected_text": None,
            "rule_used": None,
            "zone_status": "MANUAL",
            "reason": "all_engines_failed",
        }

    # ── Sort alphabetically for determinism ───────────────────────────────────
    valid_sorted = sorted(valid, key=lambda r: r.engine)

    # ── Step 1: Majority ──────────────────────────────────────────────────────
    groups: dict[str, List[ZoneEngineResult]] = {}
    for r in valid_sorted:
        norm = normalize_for_consensus(r.text)
        if norm not in groups:
            groups[norm] = []
        groups[norm].append(r)

    best_group_size = max(len(g) for g in groups.values())

    if best_group_size >= 2:
        # Select group with highest size (tie impossible with max 3 engines
        # but handle gracefully: pick group whose members are alphabetically first)
        majority_groups = [
            (norm, g) for norm, g in groups.items() if len(g) == best_group_size
        ]
        # Deterministic: sort by normalized text
        majority_groups.sort(key=lambda x: x[0])
        _, winning_group = majority_groups[0]
        # From winning group, pick alphabetically first engine
        winner = min(winning_group, key=lambda r: r.engine)
        return _make_result(winner, "majority", valid_sorted)

    # ── Step 2: Best confidence ───────────────────────────────────────────────
    with_conf = [
        r for r in valid_sorted if _valid_confidence(r.confidence) is not None
    ]

    if with_conf:
        # Highest confidence, then lex-smaller normalized text, then lex-smaller engine
        winner = min(
            with_conf,
            key=lambda r: (
                -_valid_confidence(r.confidence),
                normalize_for_consensus(r.text),
                r.engine,
            ),
        )
        return _make_result(winner, "best_confidence", valid_sorted)

    # ── Step 3: No confidence fallback ────────────────────────────────────────
    # Longest text, then lex-smaller text, then lex-smaller engine
    winner = min(
        valid_sorted,
        key=lambda r: (
            -len(r.text.strip()),
            r.text,
            r.engine,
        ),
    )
    return _make_result(winner, "no_confidence_fallback", valid_sorted)


def _make_result(
    winner: ZoneEngineResult,
    rule_used: str,
    valid_sorted: List[ZoneEngineResult],
) -> dict:
    """
    Evaluate status according to strict priority order:
    1. no_engines_configured  (handled before calling this)
    2. all_engines_failed     (handled before calling this)
    3. no_consensus           (rule_used == no_confidence_fallback)
    4. low_confidence         (confidence < 0.70)
    5. OK
    """
    if rule_used == "no_confidence_fallback":
        return {
            "selected_engine": winner.engine,
            "selected_text": winner.text,
            "rule_used": rule_used,
            "zone_status": "MANUAL",
            "reason": "no_consensus",
        }

    conf = _valid_confidence(winner.confidence)
    if conf is not None and conf < 0.70:
        return {
            "selected_engine": winner.engine,
            "selected_text": winner.text,
            "rule_used": rule_used,
            "zone_status": "MANUAL",
            "reason": "low_confidence",
        }

    return {
        "selected_engine": winner.engine,
        "selected_text": winner.text,
        "rule_used": rule_used,
        "zone_status": "OK",
        "reason": None,
    }
