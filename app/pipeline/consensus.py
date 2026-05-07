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

C7: rule_used values:
  majority_2_of_3   — 3 engines configured, 2+ matched
  match_2           — 2 engines configured, both matched
  best_confidence   — no majority, pick by confidence
  no_confidence_fallback — no match and no confidence

Normalization for engine-vs-engine comparison is delegated to the unified
`app.normalizer.normalize(text, level="consensus")`. Behavior matches the
previous in-file implementation byte-for-byte.
"""
from __future__ import annotations

from typing import List, Optional

from app.normalizer import normalize
from .ocr_dispatcher import ZoneEngineResult


def normalize_for_consensus(text: str) -> str:
    """Steps per Phase 4 Canonical v3 §5 (delegates to unified module).

    1. Lowercase (Unicode-aware).
    2. Collapse whitespace (\\s+ -> single space).
    3. Remove ASCII punctuation only; preserves backtick, %, <, >, [, ].
    4. Strip leading/trailing whitespace.
    """
    return normalize(text, level="consensus")


def _valid_confidence(confidence) -> Optional[float]:
    """Return confidence as float if 0.0 <= conf <= 1.0, else None."""
    if confidence is None:
        return None
    try:
        c = float(confidence)
    except (TypeError, ValueError):
        return None
    if 0.0 <= c <= 1.0:
        return c
    return None


def _pick_from_group(group: List[ZoneEngineResult]) -> ZoneEngineResult:
    """Pick the representative engine from a majority group.

    Deterministic rule (Phase 4 Canonical v3 §6, B1):
      (1) highest valid confidence in [0.0, 1.0]
      (2) lexicographically smaller engine name
    """
    with_conf = [(r, _valid_confidence(r.confidence)) for r in group
                 if _valid_confidence(r.confidence) is not None]

    if with_conf:
        with_conf.sort(key=lambda x: (-x[1], x[0].engine))
        return with_conf[0][0]
    return min(group, key=lambda r: r.engine)


def resolve_consensus(
    engine_results: List[ZoneEngineResult],
    engines_configured: bool,
) -> dict:
    """Resolve consensus from engine results."""
    if not engines_configured:
        return {
            "selected_engine": None,
            "selected_text": None,
            "rule_used": None,
            "zone_status": "MANUAL",
            "reason": "no_engines_configured",
        }

    valid: List[ZoneEngineResult] = [r for r in engine_results if r.error is None]

    if not valid:
        return {
            "selected_engine": None,
            "selected_text": None,
            "rule_used": None,
            "zone_status": "MANUAL",
            "reason": "all_engines_failed",
        }

    valid_sorted = sorted(valid, key=lambda r: r.engine)
    total_configured = len(engine_results)

    groups: dict[str, List[ZoneEngineResult]] = {}
    for r in valid_sorted:
        norm = normalize_for_consensus(r.text)
        groups.setdefault(norm, []).append(r)

    best_group_size = max(len(g) for g in groups.values())

    if best_group_size >= 2:
        majority_groups = [
            (norm, g) for norm, g in groups.items() if len(g) == best_group_size
        ]
        majority_groups.sort(key=lambda x: x[0])
        _, winning_group = majority_groups[0]

        if total_configured >= 3:
            rule = "majority_2_of_3"
        else:
            rule = "match_2"

        winner = _pick_from_group(winning_group)
        return _make_result(winner, rule)

    with_conf = [
        r for r in valid_sorted if _valid_confidence(r.confidence) is not None
    ]

    if with_conf:
        winner = min(
            with_conf,
            key=lambda r: (
                -_valid_confidence(r.confidence),
                normalize_for_consensus(r.text),
                r.engine,
            ),
        )
        return _make_result(winner, "best_confidence")

    winner = min(
        valid_sorted,
        key=lambda r: (
            -len(r.text.strip()),
            r.text,
            r.engine,
        ),
    )
    return _make_result(winner, "no_confidence_fallback")


def _make_result(winner: ZoneEngineResult, rule_used: str) -> dict:
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
