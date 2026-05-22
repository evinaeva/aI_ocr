"""
LLM-judge usage metrics: per-month call count and accumulated cost.

Mirrors `app.metrics.engine_usage` but for the LLM adjudicator. Two
fields per monthly document:
  - llm_calls: int (total successful calls)
  - llm_cost_usd: float (sum of `cost` reported by OpenRouter)

Both are bumped atomically per call via Firestore Increment. Non-blocking:
if Firestore is unavailable, the OCR/judge flow continues — only a
WARNING is logged.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.pipeline.firestore_store import get_db, is_persistence_enabled

logger = logging.getLogger(__name__)

_COLLECTION = "llm_judge_usage_monthly"
_UNAVAILABLE_WARN_EMITTED = False


def _current_month_id_utc(now: Optional[datetime] = None) -> str:
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%Y-%m")


def _current_month_label_utc(now: Optional[datetime] = None) -> str:
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%B %Y")


def _warn_unavailable_once(reason: str) -> None:
    global _UNAVAILABLE_WARN_EMITTED
    if _UNAVAILABLE_WARN_EMITTED:
        return
    _UNAVAILABLE_WARN_EMITTED = True
    logger.warning(
        '{"event":"llm_usage_metrics_unavailable","component":"llm_judge_usage_monthly",'
        '"reason":"%s"}',
        reason,
    )


def init_llm_usage_metrics() -> None:
    """Called once at startup; emits a warning if Firestore is not enabled."""
    if not is_persistence_enabled():
        _warn_unavailable_once("firestore_unavailable_or_disabled")


def increment_llm_usage(cost_usd: float = 0.0, delta: int = 1) -> None:
    """Atomically bump LLM call count + cost for the current UTC month."""
    if delta <= 0:
        return

    if not is_persistence_enabled():
        _warn_unavailable_once("firestore_unavailable_or_disabled")
        return

    safe_cost = max(0.0, float(cost_usd) if cost_usd is not None else 0.0)
    month_id = _current_month_id_utc()
    try:
        from google.cloud import firestore as _fs  # type: ignore

        doc_ref = get_db().collection(_COLLECTION).document(month_id)
        doc_ref.set(
            {
                "llm_calls": _fs.Increment(delta),
                "llm_cost_usd": _fs.Increment(safe_cost),
                "updated_at_utc": _fs.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        logger.info(
            '{"event":"llm_usage_increment","delta":%d,"cost_usd":%.6f,"month_id":"%s"}',
            delta,
            safe_cost,
            month_id,
        )
    except Exception as exc:
        logger.warning(
            '{"event":"llm_usage_increment_failed","month_id":"%s","error":"%s"}',
            month_id,
            str(exc)[:200],
        )


def get_current_month_llm_usage() -> dict:
    """Return current UTC month's LLM judge usage. Always returns month_id/label."""
    month_id = _current_month_id_utc()
    payload = {
        "month_id": month_id,
        "month_label": _current_month_label_utc(),
        "llm_calls": None,
        "llm_cost_usd": None,
        "available": False,
    }

    if not is_persistence_enabled():
        _warn_unavailable_once("firestore_unavailable_or_disabled")
        return payload

    try:
        snap = get_db().collection(_COLLECTION).document(month_id).get()
        data = snap.to_dict() if snap.exists else {}
        calls_val = int(data.get("llm_calls", 0))
        cost_val = float(data.get("llm_cost_usd", 0.0))
        payload.update(
            {
                "llm_calls": calls_val,
                "llm_cost_usd": cost_val,
                "available": True,
            }
        )
        logger.info(
            '{"event":"llm_usage_read","month_id":"%s","doc_exists":%s,'
            '"calls":%d,"cost_usd":%.6f,"available":true}',
            month_id,
            str(snap.exists).lower(),
            calls_val,
            cost_val,
        )
    except Exception as exc:
        logger.warning(
            '{"event":"llm_usage_read_failed","month_id":"%s","error":"%s"}',
            month_id,
            str(exc)[:200],
        )

    return payload
