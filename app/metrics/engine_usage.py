from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.pipeline.firestore_store import get_db, is_persistence_enabled

logger = logging.getLogger(__name__)

_COLLECTION = "ocr_engine_usage_monthly"
_WARN_EMITTED = False


def _current_month_id_utc(now: Optional[datetime] = None) -> str:
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%Y-%m")


def _current_month_label_utc(now: Optional[datetime] = None) -> str:
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%B %Y")


def _warn_unavailable_once(reason: str) -> None:
    global _WARN_EMITTED
    if _WARN_EMITTED:
        return
    _WARN_EMITTED = True
    logger.warning(
        '{"event":"engine_usage_metrics_unavailable","component":"ocr_engine_usage_monthly",'
        '"reason":"%s"}',
        reason,
    )


def init_engine_usage_metrics() -> None:
    if not is_persistence_enabled():
        _warn_unavailable_once("firestore_unavailable_or_disabled")


def increment_engine_usage(engine: str, delta: int = 1) -> None:
    if delta <= 0:
        return
    if engine not in ("google", "azure", "ocrspace"):
        return

    if not is_persistence_enabled():
        _warn_unavailable_once("firestore_unavailable_or_disabled")
        return

    try:
        from google.cloud import firestore as _fs  # type: ignore

        month_id = _current_month_id_utc()
        doc_ref = get_db().collection(_COLLECTION).document(month_id)
        field_map = {
            "google": "google_requests",
            "azure": "azure_requests",
            "ocrspace": "ocrspace_requests",
        }
        doc_ref.set(
            {
                field_map[engine]: _fs.Increment(delta),
                "updated_at_utc": _fs.SERVER_TIMESTAMP,
            },
            merge=True,
        )
    except Exception:
        _warn_unavailable_once("firestore_write_failed")


def get_current_month_usage() -> dict:
    month_id = _current_month_id_utc()
    payload = {
        "month_id": month_id,
        "month_label": _current_month_label_utc(),
        "google_requests": None,
        "azure_requests": None,
        "ocrspace_requests": None,
        "available": False,
    }

    if not is_persistence_enabled():
        _warn_unavailable_once("firestore_unavailable_or_disabled")
        return payload

    try:
        snap = get_db().collection(_COLLECTION).document(month_id).get()
        data = snap.to_dict() if snap.exists else {}
        payload.update(
            {
                "google_requests": int(data.get("google_requests", 0)),
                "azure_requests": int(data.get("azure_requests", 0)),
                "ocrspace_requests": int(data.get("ocrspace_requests", 0)),
                "available": True,
            }
        )
    except Exception:
        _warn_unavailable_once("firestore_read_failed")

    return payload
