"""
Phase 2 additive routes.
Uses existing legacy worker `_process_session` from app.main.
"""
from __future__ import annotations

import time
import json
import logging
from typing import Dict, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from fastapi.responses import JSONResponse

phase2_router = APIRouter()
logger = logging.getLogger(__name__)


class Phase2RunRequest(BaseModel):
    template_name: Optional[str] = None


def _resolve_target_bboxes(template_name: Optional[str]) -> Dict[str, list[int]]:
    if not template_name:
        return {}

    from app import main as main_module

    tmpl = main_module.template_store.get_template(template_name)
    if tmpl is None:
        logger.warning("phase2_run template_not_found template_name=%s", template_name)
        return {}

    target_bboxes: Dict[str, list[int]] = {}
    for zone in tmpl.zones:
        if getattr(zone, "type", "") != "ocr":
            continue
        notes = getattr(zone, "notes", "")
        if not isinstance(notes, str) or not notes.strip():
            continue
        try:
            meta = json.loads(notes)
        except Exception:
            continue
        target_id = meta.get("phase2_target_id") if isinstance(meta, dict) else None
        if not isinstance(target_id, str) or not target_id.strip():
            continue
        bbox = main_module._resolve_real_bbox(zone)
        if bbox is None:
            continue
        target_bboxes.setdefault(target_id, bbox)
    logger.info(
        "phase2_run_bbox_mapping template_name=%s targets_with_bbox=%d",
        template_name,
        len(target_bboxes),
    )
    return target_bboxes


@phase2_router.post("/api/phase2/run/{upload_id}")
async def phase2_run(upload_id: str, body: Optional[Phase2RunRequest] = None):
    # Imported lazily to avoid module import cycle with app.main router wiring.
    from app import main as main_module

    conn = main_module.get_db()
    row = conn.execute(
        "SELECT zip_bytes, section_number, section_name, created_at FROM phase2_uploads WHERE upload_id=?",
        (upload_id,),
    ).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "upload not found"}, status_code=404)
    if row["created_at"] < time.time() - main_module.SESSION_TTL_SECONDS:
        conn.close()
        return JSONResponse({"error": "upload expired"}, status_code=410)

    engines = ["google", "azure", "ocrspace"]
    zip_bytes = bytes(row["zip_bytes"])
    section_number = row["section_number"]
    section_name = row["section_name"]
    conn.close()
    target_bboxes = _resolve_target_bboxes(body.template_name if body is not None else None)

    session_id = main_module._start_session_from_zip(
        zip_bytes,
        section_number,
        section_name,
        engines,
        target_bboxes=target_bboxes,
    )
    return JSONResponse({"session_id": session_id})
