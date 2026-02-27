"""
Phase 2 additive routes.
Uses existing legacy worker `_process_session` from app.main.
"""
from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

phase2_router = APIRouter()


@phase2_router.post("/api/phase2/run/{upload_id}")
async def phase2_run(upload_id: str):
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

    session_id = main_module._start_session_from_zip(
        zip_bytes,
        section_number,
        section_name,
        engines,
    )
    return JSONResponse({"session_id": session_id})
