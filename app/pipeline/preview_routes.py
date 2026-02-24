"""
Phase 3 — Preview Crops routes.

HTML: GET /templates/preview  (defined here, included via preview_router)
API:  POST /api/templates/{template_name}/preview-crops
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from . import template_store
from .image_processor import (
    crop_to_base64,
    load_image,
    maybe_upscale,
    pillow_available,
    scale_bbox,
)

preview_router = APIRouter()

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_jinja = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@preview_router.get("/templates/preview", response_class=HTMLResponse)
async def preview_crops_page(request: Request):
    template_names = template_store.list_templates()
    return _jinja.TemplateResponse(
        "preview_crops.html",
        {"request": request, "template_names": template_names},
    )


@preview_router.post("/api/templates/{template_name}/preview-crops")
async def preview_crops_api(
    template_name: str,
    image: UploadFile = File(...),
):
    if not pillow_available():
        return JSONResponse(
            {"error": "pillow_missing", "details": "Pillow is not installed"},
            status_code=500,
        )

    # Resolve template
    tmpl = template_store.get_template(template_name)
    if tmpl is None:
        return JSONResponse(
            {"error": "not_found", "details": "template_not_found"},
            status_code=404,
        )

    # Load image
    image_bytes = await image.read()
    try:
        img_original = load_image(image_bytes)
    except ValueError:
        return JSONResponse(
            {"error": "invalid_input", "details": "invalid_image"},
            status_code=400,
        )

    original_w, original_h = img_original.size
    source_w, source_h = tmpl.source_size  # template source size

    # Step 1: maybe_upscale
    img_processed, upscaled = maybe_upscale(img_original)
    processed_w, processed_h = img_processed.size

    # Step 2: per-zone scale + crop
    zones_out = []
    for zone in tmpl.zones:
        bbox_scaled: List[int] = scale_bbox(
            zone.bbox,
            source_size=[source_w, source_h],
            actual_size=[processed_w, processed_h],  # ALWAYS processed size
        )
        crop_b64 = crop_to_base64(img_processed, bbox_scaled)
        zones_out.append(
            {
                "zone_name": zone.name,
                "bbox_source": zone.bbox,
                "bbox_scaled": bbox_scaled,
                "crop_png_base64": crop_b64,
            }
        )

    return JSONResponse(
        {
            "template_name": template_name,
            "source_size": [source_w, source_h],
            "original_size": [original_w, original_h],
            "upscaled": upscaled,
            "processed_size": [processed_w, processed_h],
            "zones": zones_out,
        }
    )
