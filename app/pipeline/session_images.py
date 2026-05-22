"""
Session-image persistence — GCS-backed so thumbnails survive across
Cloud Run instances.

Cloud Run is horizontally autoscaled: each instance has its own ephemeral
`/tmp/sessions.db`. Without this module, the `images` SQLite blob written
by `_process_session` on instance A is invisible to a request served by
instance B — so the `/image/{session_id}/{filename}` endpoint returns 404
about half the time once Cloud Run scales past one replica. Operators
report it as "thumbnails don't render".

We mirror every saved image to GCS, keyed by `sessions/{session_id}/{filename}`.
The image endpoint then falls through to GCS on local DB miss.

Bucket: configured via `SESSION_IMAGES_GCS_BUCKET` env var. If unset, the
GCS path is skipped (single-instance dev mode). Lifecycle on the bucket
can be set to auto-delete after N days — we don't need session images
forever.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_BUCKET_ENV = "SESSION_IMAGES_GCS_BUCKET"
_DEFAULT_PREFIX = "sessions"

_BUCKET_NAME: Optional[str] = None
_CLIENT = None
_AVAILABILITY_WARNED = False


def _get_bucket():
    """Return a `google.cloud.storage.Bucket` or None if unconfigured."""
    global _BUCKET_NAME, _CLIENT, _AVAILABILITY_WARNED
    if _BUCKET_NAME is None:
        _BUCKET_NAME = os.getenv(_BUCKET_ENV, "").strip() or ""
    if not _BUCKET_NAME:
        if not _AVAILABILITY_WARNED:
            logger.info(
                'session_images_gcs disabled: %s is unset', _BUCKET_ENV,
            )
            _AVAILABILITY_WARNED = True
        return None
    if _CLIENT is None:
        try:
            from google.cloud import storage  # type: ignore
            _CLIENT = storage.Client()
        except Exception as exc:
            logger.warning("session_images_gcs client init failed: %s", exc)
            return None
    try:
        return _CLIENT.bucket(_BUCKET_NAME)
    except Exception as exc:
        logger.warning("session_images_gcs bucket() failed: %s", exc)
        return None


def _key(session_id: str, filename: str) -> str:
    safe = filename.replace("..", "_")
    return f"{_DEFAULT_PREFIX}/{session_id}/{safe}"


def _content_type_for(filename: str) -> str:
    low = filename.lower()
    if low.endswith(".png"):
        return "image/png"
    if low.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if low.endswith(".gif"):
        return "image/gif"
    if low.endswith(".webp"):
        return "image/webp"
    return "application/octet-stream"


def put_session_image(session_id: str, filename: str, image_bytes: bytes) -> None:
    """Upload an image to GCS for cross-instance access. No-op if unconfigured.

    Fire-and-forget: a write failure must not block the OCR pipeline. We log
    the error and continue — the local SQLite blob is still authoritative
    for the instance that processed the session.
    """
    bucket = _get_bucket()
    if bucket is None:
        return
    try:
        blob = bucket.blob(_key(session_id, filename))
        blob.upload_from_string(image_bytes, content_type=_content_type_for(filename))
    except Exception as exc:
        logger.warning(
            "session_image_upload_failed session_id=%s filename=%s error=%s",
            session_id, filename, str(exc)[:200],
        )


def get_session_image(session_id: str, filename: str) -> Optional[bytes]:
    """Fetch an image from GCS. Returns None if unconfigured or missing."""
    bucket = _get_bucket()
    if bucket is None:
        return None
    try:
        blob = bucket.blob(_key(session_id, filename))
        if not blob.exists():
            return None
        return blob.download_as_bytes()
    except Exception as exc:
        logger.warning(
            "session_image_download_failed session_id=%s filename=%s error=%s",
            session_id, filename, str(exc)[:200],
        )
        return None
