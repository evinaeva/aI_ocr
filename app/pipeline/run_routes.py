"""
Phase 4: Run routes.

POST /api/templates/{template_name}/run
  - Accepts: multipart/form-data with image file
  - Returns: JSON run result
"""
from __future__ import annotations

import io
import time
import uuid
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

from app.logging_utils import log_event
from app.pipeline import template_store
from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr
from app.pipeline.consensus import resolve_consensus

run_router = APIRouter()


def _crop_zone(image_bytes: bytes, zone: ZoneDef, source_size: list) -> bytes:
    """
    Crop image to zone bbox, scaling bbox coords to actual image size.
    Returns JPEG bytes.
    """
    img = Image.open(io.BytesIO(image_bytes))
    img_w, img_h = img.size
    src_w, src_h = source_size[0], source_size[1]

    scale_x = img_w / src_w
    scale_y = img_h / src_h

    x1, y1, x2, y2 = zone.bbox
    px1 = int(x1 * scale_x)
    py1 = int(y1 * scale_y)
    px2 = int(x2 * scale_x)
    py2 = int(y2 * scale_y)

    # Clamp to image bounds
    px1 = max(0, min(px1, img_w))
    py1 = max(0, min(py1, img_h))
    px2 = max(0, min(px2, img_w))
    py2 = max(0, min(py2, img_h))

    cropped = img.crop((px1, py1, px2, py2))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG")
    return buf.getvalue()


@run_router.post("/api/templates/{template_name}/run")
async def run_template(
    template_name: str,
    image: UploadFile = File(...),
):
    """
    Run per-zone OCR + consensus for all zones in the template.

    Returns JSON:
    {
      "run_id": "...",
      "template_name": "...",
      "zones": [
        {
          "zone_name": "...",
          "engines_used": [...],
          "engine_results": [...],
          "consensus": {...}
        },
        ...
      ]
    }
    """
    run_id = str(uuid.uuid4())
    try:
        # Load template
        tmpl = template_store.get_template(template_name)
        if tmpl is None:
            raise HTTPException(status_code=404, detail="Template not found")

        image_bytes = await image.read()

        log_event("run_start", run_id=run_id, template_name=template_name)

        zone_results = []

        for zone in tmpl.zones:
            engines_configured = len(zone.engines) > 0

            if not engines_configured:
                # No engines: skip OCR, send empty to consensus
                engine_results = []
                consensus = resolve_consensus(
                    engine_results=[],
                    engines_configured=False,
                )
                zone_results.append({
                    "zone_name": zone.name,
                    "engines_used": zone.engines,
                    "engine_results": [],
                    "consensus": consensus,
                })
                continue

            # Crop zone from image
            try:
                zone_bytes = _crop_zone(image_bytes, zone, tmpl.source_size)
            except Exception as exc:
                # If crop fails, use full image
                zone_bytes = image_bytes

            # Dispatch OCR for this zone
            engine_results = dispatch_zone_ocr(zone, zone_bytes)

            # Resolve consensus
            consensus = resolve_consensus(
                engine_results=engine_results,
                engines_configured=True,
            )

            zone_results.append({
                "zone_name": zone.name,
                "engines_used": zone.engines,
                "engine_results": [r.to_dict() for r in engine_results],
                "consensus": consensus,
            })

        log_event("run_end", run_id=run_id, status="ok")

        return JSONResponse({
            "run_id": run_id,
            "template_name": template_name,
            "zones": zone_results,
        })

    except HTTPException:
        raise
    except Exception as exc:
        log_event("run_end", run_id=run_id, status="error")
        return JSONResponse({"error": "internal_error"}, status_code=500)
