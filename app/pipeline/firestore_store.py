"""
Phase 6: Firestore client initialisation with hard gate.

Hard gate (§0.1): if google-cloud-firestore is not installed,
FIRESTORE_CLIENT_AVAILABLE = False and the rest of the module exposes no client.
No fallback, no REST alternative — callers must check PERSISTENCE_ENABLED.

Variant A (Phase 6 fix):
  FIRESTORE_CLIENT_AVAILABLE  — True iff the library is importable
  PERSISTENCE_ENABLED         — True iff library importable AND env FIRESTORE_AVAILABLE=true
  FIRESTORE_AVAILABLE         — legacy alias == FIRESTORE_CLIENT_AVAILABLE (backwards compat)

Ops: ensure Cloud Run has GOOGLE_CLOUD_PROJECT=<project-id> set via:
  gcloud run services update ai-ocr --region europe-west1 \\
    --project <project-id> --update-env-vars GOOGLE_CLOUD_PROJECT=<project-id>
"""
from __future__ import annotations

import os

# ── 1. Can we import the client library? ─────────────────────────────────────
FIRESTORE_CLIENT_AVAILABLE: bool = False
_db = None

try:
    from google.cloud import firestore as _firestore  # type: ignore
    FIRESTORE_CLIENT_AVAILABLE = True
except ImportError:
    _firestore = None  # type: ignore

# Legacy alias — kept for backwards compatibility; equals FIRESTORE_CLIENT_AVAILABLE.
FIRESTORE_AVAILABLE: bool = FIRESTORE_CLIENT_AVAILABLE


# ── 2. Is persistence enabled for this deployment? ───────────────────────────
def is_persistence_enabled() -> bool:
    """Return True iff FIRESTORE_AVAILABLE env var is 'true' AND the client library is present."""
    env_flag = os.environ.get("FIRESTORE_AVAILABLE", "").strip().lower() == "true"
    return FIRESTORE_CLIENT_AVAILABLE and env_flag


# Module-level flag — convenience; callers can also call is_persistence_enabled().
PERSISTENCE_ENABLED: bool = is_persistence_enabled()


# ── 3. Client helpers ─────────────────────────────────────────────────────────
def get_db():
    """
    Return a Firestore client (lazy singleton).
    Raises RuntimeError if Firestore client library is not available.

    Project resolution: reads GOOGLE_CLOUD_PROJECT (preferred) or GCLOUD_PROJECT env var
    and passes it explicitly to firestore.Client() to avoid cross-project writes after
    a new Cloud Run revision rollout. Falls back to default (ADC) when neither is set.
    Ops: set GOOGLE_CLOUD_PROJECT in Cloud Run env vars (see module docstring).
    """
    global _db
    if not FIRESTORE_CLIENT_AVAILABLE:
        raise RuntimeError("google-cloud-firestore is not installed")
    if _db is None:
        # Read project id from env to pin Firestore to the correct GCP project.
        # Do NOT hardcode the project id here — set GOOGLE_CLOUD_PROJECT in Cloud Run.
        project = (
            os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
            or os.environ.get("GCLOUD_PROJECT", "").strip()
            or None
        )
        _db = _firestore.Client(project=project) if project else _firestore.Client()
    return _db


def server_timestamp():
    """Return Firestore SERVER_TIMESTAMP sentinel."""
    if not FIRESTORE_CLIENT_AVAILABLE:
        raise RuntimeError("google-cloud-firestore is not installed")
    return _firestore.SERVER_TIMESTAMP


def firestore_timestamp_to_iso(ts) -> str:
    """
    Convert a Firestore Timestamp (or datetime) to ISO 8601 UTC string.
    Format: YYYY-MM-DDTHH:MM:SSZ (no microseconds, per §8).
    """
    from datetime import timezone
    if ts is None:
        return ""
    if hasattr(ts, "seconds"):
        from datetime import datetime
        dt = datetime.fromtimestamp(ts.seconds, tz=timezone.utc)
    else:
        dt = ts.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
