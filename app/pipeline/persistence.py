"""
Phase 6: Atomic persistence of run results to Firestore.

Collections:
  template_runs            — one header doc per run
  template_run_zones       — one doc per zone, id = {run_id}__{zone_index}

All writes are done in a single batched commit (atomic, §1.2).
Timestamps stored as Firestore Timestamp (§0.2 / §8).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from google.api_core.exceptions import FailedPrecondition  # type: ignore

from app.logging_utils import log_event
from app.pipeline.firestore_store import FIRESTORE_AVAILABLE, get_db

COLLECTION_RUNS = "template_runs"
COLLECTION_ZONES = "template_run_zones"


def _derive_ocr_overall_status(zones: List[Dict[str, Any]]) -> str:
    """OK if all zones are OK, else MANUAL."""
    for z in zones:
        consensus = z.get("consensus") or {}
        if consensus.get("zone_status") != "OK":
            return "MANUAL"
    return "OK"


def persist_run(
    run_id: str,
    template_name: str,
    lang: Optional[str],
    zones: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Persist run header + all zone docs in one atomic batch commit.

    Returns dict with:
      persisted: bool
      persistence_error: bool
      persistence_error_type: str | None

    On commit failure → persisted=False, persistence_error=True,
    persistence_error_type="firestore_write_failed".
    Nothing is left partially written (Firestore batch is atomic).
    """
    if not FIRESTORE_AVAILABLE:
        return {
            "persisted": False,
            "persistence_error": True,
            "persistence_error_type": "firestore_write_failed",
        }

    try:
        db = get_db()

        # Import Timestamp for stored fields (§0.2)
        from google.cloud import firestore as _fs  # type: ignore

        zones_count = len(zones)
        ocr_overall_status = _derive_ocr_overall_status(zones)

        # Build batch
        batch = db.batch()

        # ── Header doc ────────────────────────────────────────────────────────
        header_ref = db.collection(COLLECTION_RUNS).document(run_id)
        header_data = {
            "run_id": run_id,
            "template_name": template_name,
            "lang": lang or None,
            "created_at": _fs.SERVER_TIMESTAMP,
            "zones_count": zones_count,
            "ocr_overall_status": ocr_overall_status,
            "review_overall_status": "PENDING",
            "review_counts": {
                "pending": zones_count,
                "approved": 0,
                "rejected": 0,
            },
        }
        batch.set(header_ref, header_data)

        # ── Zone docs ─────────────────────────────────────────────────────────
        for idx, zone in enumerate(zones):
            zone_doc_id = f"{run_id}__{idx}"
            zone_ref = db.collection(COLLECTION_ZONES).document(zone_doc_id)
            zone_data = {
                "run_id": run_id,
                "template_name": template_name,
                "zone_index": idx,
                "zone_name": zone.get("zone_name", ""),
                # Entire zone dict from /run preserved verbatim (§2.1)
                "run_zone_payload": zone,
                "review": {
                    "review_status": "PENDING",
                    "review_comment": None,
                    "reviewed_at": None,
                },
            }
            batch.set(zone_ref, zone_data)

        # Single atomic commit (§1.2 / §3.1)
        batch.commit()

        log_event("run_persisted", run_id=run_id, template_name=template_name)
        return {
            "persisted": True,
            "persistence_error": False,
            "persistence_error_type": None,
        }

    except FailedPrecondition as exc:
        # Missing Firestore index — log with full diagnostic
        log_event(
            "persistence_error",
            run_id=run_id,
            error_type="firestore_index_missing",
            exc_type=type(exc).__name__,
            message=str(exc),
        )
        return {
            "persisted": False,
            "persistence_error": True,
            "persistence_error_type": "firestore_write_failed",
        }
    except Exception as exc:  # noqa: BLE001
        log_event(
            "persistence_error",
            run_id=run_id,
            error_type="firestore_write_failed",
            exc_type=type(exc).__name__,
            message=str(exc),
        )
        return {
            "persisted": False,
            "persistence_error": True,
            "persistence_error_type": "firestore_write_failed",
        }
