from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from app.pipeline.firestore_store import get_db, is_persistence_enabled

logger = logging.getLogger(__name__)

_COLLECTION = "ocr_engine_usage_monthly"
# Guard to emit the 'firestore disabled/unavailable' warning only once per process.
# NOTE: this guard is intentionally NOT used for write/read failures — those must
# be logged every time so failures remain visible in Cloud Run logs after redeploys.
_UNAVAILABLE_WARN_EMITTED = False


def _current_month_id_utc(now: Optional[datetime] = None) -> str:
    """Return YYYY-MM string for the current UTC month."""
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%Y-%m")


def _current_month_label_utc(now: Optional[datetime] = None) -> str:
    """Return human-readable label (e.g. 'March 2026') for the current UTC month."""
    dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return dt.strftime("%B %Y")


def _warn_unavailable_once(reason: str) -> None:
    """Emit 'Firestore disabled/unavailable' warning at most once per process lifetime."""
    global _UNAVAILABLE_WARN_EMITTED
    if _UNAVAILABLE_WARN_EMITTED:
        return
    _UNAVAILABLE_WARN_EMITTED = True
    logger.warning(
        '{"event":"engine_usage_metrics_unavailable","component":"ocr_engine_usage_monthly",'
        '"reason":"%s"}',
        reason,
    )


def init_engine_usage_metrics() -> None:
    """Called once at startup; emits a warning if Firestore is not enabled."""
    if not is_persistence_enabled():
        _warn_unavailable_once("firestore_unavailable_or_disabled")


_FIELD_MAP = {
    "google": "google_requests",
    "azure": "azure_requests",
    "ocrspace": "ocrspace_requests",
}


def increment_engine_usage(engine: str, delta: int = 1) -> None:
    """
    Atomically increment the monthly usage counter for *engine* in Firestore.

    Non-blocking: if Firestore is unavailable or the write fails, the OCR flow
    continues — only a WARNING is logged.

    Logging:
      - On success: INFO event 'engine_usage_increment'.
      - On write failure: WARNING event 'engine_usage_increment_failed' (every time,
        not just the first — so failures remain visible in Cloud Run logs).
    """
    if delta <= 0:
        return
    if engine not in _FIELD_MAP:
        return

    if not is_persistence_enabled():
        _warn_unavailable_once("firestore_unavailable_or_disabled")
        return

    month_id = _current_month_id_utc()
    try:
        from google.cloud import firestore as _fs  # type: ignore

        doc_ref = get_db().collection(_COLLECTION).document(month_id)
        doc_ref.set(
            {
                _FIELD_MAP[engine]: _fs.Increment(delta),
                "updated_at_utc": _fs.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        # Emit a structured INFO log on every successful write so that writes are
        # observable in Cloud Run logs and persistence can be verified without
        # querying Firestore directly.
        logger.info(
            '{"event":"engine_usage_increment","engine":"%s","delta":%d,"month_id":"%s"}',
            engine,
            delta,
            month_id,
        )
    except Exception as exc:
        # Log EVERY write failure — do NOT use _warn_unavailable_once here.
        # After a new Cloud Run revision, the first failure must not silence
        # subsequent ones; all failures must appear in logs.
        logger.warning(
            '{"event":"engine_usage_increment_failed","engine":"%s","month_id":"%s","error":"%s"}',
            engine,
            month_id,
            str(exc)[:200],
        )


def get_current_month_usage() -> dict:
    """
    Return the current UTC month's engine usage counters from Firestore.

    Always returns month_id and month_label (UTC-based).
    - If Firestore is disabled: available=False, counts=None.
    - If doc is missing: available=True, counts=0 (not an error — first use of the month).
    - If read fails: available=False, counts=None, WARNING logged every time.
    """
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
        google_val = int(data.get("google_requests", 0))
        azure_val = int(data.get("azure_requests", 0))
        ocrspace_val = int(data.get("ocrspace_requests", 0))
        payload.update(
            {
                "google_requests": google_val,
                "azure_requests": azure_val,
                "ocrspace_requests": ocrspace_val,
                "available": True,
            }
        )
        # Emit a structured INFO log for every read so the endpoint's data source
        # and the actual Firestore values are visible in Cloud Run logs.
        logger.info(
            '{"event":"engine_usage_read","month_id":"%s","doc_exists":%s,'
            '"google":%d,"azure":%d,"ocrspace":%d,"available":true}',
            month_id,
            str(snap.exists).lower(),
            google_val,
            azure_val,
            ocrspace_val,
        )
    except Exception as exc:
        # Log every read failure — not just the first — so issues are always visible.
        logger.warning(
            '{"event":"engine_usage_read_failed","month_id":"%s","error":"%s"}',
            month_id,
            str(exc)[:200],
        )

    return payload
