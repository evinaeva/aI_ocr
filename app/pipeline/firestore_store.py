"""
Phase 6: Firestore client initialisation with hard gate.

Hard gate (§0.1): if google-cloud-firestore is not installed,
FIRESTORE_AVAILABLE = False and the rest of the module exposes no client.
No fallback, no REST alternative — callers must check FIRESTORE_AVAILABLE.
"""
from __future__ import annotations

FIRESTORE_AVAILABLE: bool = False
_db = None

try:
    from google.cloud import firestore as _firestore  # type: ignore
    FIRESTORE_AVAILABLE = True
except ImportError:
    _firestore = None  # type: ignore


def get_db():
    """
    Return a Firestore client (lazy singleton).
    Raises RuntimeError if Firestore is not available.
    """
    global _db
    if not FIRESTORE_AVAILABLE:
        raise RuntimeError("google-cloud-firestore is not installed")
    if _db is None:
        _db = _firestore.Client()
    return _db


def server_timestamp():
    """Return Firestore SERVER_TIMESTAMP sentinel."""
    if not FIRESTORE_AVAILABLE:
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
    # Firestore Timestamp has .seconds / .nanos; datetime has .timestamp()
    if hasattr(ts, "seconds"):
        from datetime import datetime
        dt = datetime.fromtimestamp(ts.seconds, tz=timezone.utc)
    else:
        # already a datetime
        dt = ts.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
