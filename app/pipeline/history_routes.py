"""
Phase 6: History + Review API endpoints.

GET  /api/templates/{template_name}/history
GET  /api/runs/{run_id}
POST /api/runs/{run_id}/zones/{zone_index}/review

Variant A fix: routers are always registered; endpoints return 503 when
PERSISTENCE_ENABLED is False, never 404.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.logging_utils import log_event
from app.pipeline.firestore_store import (
    get_db,
    firestore_timestamp_to_iso,
    is_persistence_enabled,
)
from app.pipeline.persistence import COLLECTION_RUNS, COLLECTION_ZONES

history_router = APIRouter()
logger = logging.getLogger(__name__)

_DISABLED = JSONResponse(
    {"detail": "Persistence disabled"},
    status_code=503,
)


@history_router.get("/api/templates/{template_name}/history")
async def get_history(template_name: str):
    """
    Returns up to 50 runs for a template, ordered by created_at DESC.
    Returns empty runs list (not 404) when no runs exist (§6.1).
    Returns 503 when persistence is disabled.
    """
    if not is_persistence_enabled():
        return JSONResponse({"detail": "Persistence disabled"}, status_code=503)

    try:
        db = get_db()
        query = (
            db.collection(COLLECTION_RUNS)
            .where("template_name", "==", template_name)
            .order_by("created_at", direction="DESCENDING")
            .limit(50)
        )
        docs = query.stream()
        runs = []
        for doc in docs:
            d = doc.to_dict()
            runs.append({
                "run_id": d.get("run_id", ""),
                "created_at_utc": firestore_timestamp_to_iso(d.get("created_at")),
                "lang": d.get("lang"),
                "zones_count": d.get("zones_count", 0),
                "ocr_overall_status": d.get("ocr_overall_status", "OK"),
                "review_overall_status": d.get("review_overall_status", "PENDING"),
            })
        return JSONResponse({"template_name": template_name, "runs": runs})

    except Exception:
        logger.exception("history get_history failed for template_name=%s", template_name)
        return JSONResponse({"error": "internal_error"}, status_code=500)


@history_router.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """
    Returns full run with all zones ordered by zone_index ASC.
    404 if header doc not found (§6.2).
    503 when persistence is disabled.
    """
    if not is_persistence_enabled():
        return JSONResponse({"detail": "Persistence disabled"}, status_code=503)

    try:
        db = get_db()

        header_ref = db.collection(COLLECTION_RUNS).document(run_id)
        header_snap = header_ref.get()
        if not header_snap.exists:
            return JSONResponse({"error": "not_found"}, status_code=404)

        h = header_snap.to_dict()

        zones_query = (
            db.collection(COLLECTION_ZONES)
            .where("run_id", "==", run_id)
            .order_by("zone_index")
        )
        zone_docs = zones_query.stream()
        zones = []
        for zdoc in zone_docs:
            zd = zdoc.to_dict()
            review = zd.get("review", {})
            zones.append({
                "zone_index": zd.get("zone_index"),
                "zone_name": zd.get("zone_name", ""),
                "run_zone_payload": zd.get("run_zone_payload", {}),
                "review": {
                    "review_status": review.get("review_status", "PENDING"),
                    "review_comment": review.get("review_comment"),
                    "reviewed_at": firestore_timestamp_to_iso(review.get("reviewed_at"))
                    if review.get("reviewed_at") else None,
                },
            })

        return JSONResponse({
            "run_id": h.get("run_id", run_id),
            "template_name": h.get("template_name", ""),
            "created_at_utc": firestore_timestamp_to_iso(h.get("created_at")),
            "lang": h.get("lang"),
            "zones": zones,
            "ocr_overall_status": h.get("ocr_overall_status", "OK"),
            "review_overall_status": h.get("review_overall_status", "PENDING"),
        })

    except Exception:
        logger.exception("history get_run failed for run_id=%s", run_id)
        return JSONResponse({"error": "internal_error"}, status_code=500)


@history_router.post("/api/runs/{run_id}/zones/{zone_index}/review")
async def update_zone_review(run_id: str, zone_index: int, body: dict):
    """
    Update zone review status.
    503 when persistence is disabled.

    Body: { "review_status": "APPROVED"|"REJECTED", "review_comment": "..." }
    """
    if not is_persistence_enabled():
        return JSONResponse({"detail": "Persistence disabled"}, status_code=503)

    review_status = body.get("review_status")
    if review_status not in ("APPROVED", "REJECTED"):
        return JSONResponse(
            {"error": "invalid_status", "detail": "review_status must be APPROVED or REJECTED"},
            status_code=400,
        )

    review_comment = body.get("review_comment")
    if review_comment is not None and len(str(review_comment)) > 1000:
        return JSONResponse(
            {"error": "comment_too_long", "detail": "review_comment must be \u2264 1000 chars"},
            status_code=400,
        )

    try:
        from google.cloud import firestore as _fs  # type: ignore

        db = get_db()
        header_ref = db.collection(COLLECTION_RUNS).document(run_id)
        zone_ref = db.collection(COLLECTION_ZONES).document(f"{run_id}__{zone_index}")

        @_fs.transactional
        def _do_update(transaction):
            # transaction.get() on a DocumentReference returns a generator in
            # newer Firestore client versions; consume it with next() to get the
            # actual DocumentSnapshot.
            zone_snap = next(transaction.get(zone_ref))
            header_snap = next(transaction.get(header_ref))

            if not header_snap.exists:
                return "header_not_found"
            if not zone_snap.exists:
                return "zone_not_found"

            h = header_snap.to_dict()
            z = zone_snap.to_dict()

            prev_status = z.get("review", {}).get("review_status", "PENDING")
            counts = dict(h.get("review_counts", {"pending": 0, "approved": 0, "rejected": 0}))

            if prev_status != review_status:
                counts[prev_status.lower()] = max(0, counts.get(prev_status.lower(), 0) - 1)
                counts[review_status.lower()] = counts.get(review_status.lower(), 0) + 1

            if counts.get("rejected", 0) > 0:
                overall = "REJECTED"
            elif counts.get("pending", 0) > 0:
                overall = "PENDING"
            else:
                overall = "APPROVED"

            transaction.update(zone_ref, {
                "review.review_status": review_status,
                "review.review_comment": review_comment,
                "review.reviewed_at": _fs.SERVER_TIMESTAMP,
            })

            transaction.update(header_ref, {
                "review_counts": counts,
                "review_overall_status": overall,
            })

            return "ok"

        result = _do_update(db.transaction())

        if result == "header_not_found":
            return JSONResponse({"error": "not_found"}, status_code=404)
        if result == "zone_not_found":
            return JSONResponse({"error": "zone_not_found"}, status_code=404)

        log_event(
            "zone_review_updated",
            run_id=run_id,
            zone_index=zone_index,
            new_status=review_status,
        )

        return JSONResponse({"ok": True, "review_status": review_status})

    except Exception:
        logger.exception("history update_zone_review failed for run_id=%s zone_index=%s", run_id, zone_index)
        return JSONResponse({"error": "persistence_error"}, status_code=500)
