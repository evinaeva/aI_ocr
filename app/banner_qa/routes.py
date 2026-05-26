"""FastAPI router for the local (CV-only) banner QA pipeline.

Lives at `/api/banner/*` so it sits next to `/api/templates/*` (OCR LLM)
without colliding with the existing routes.
"""
from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

from app.logging_utils import log_event

from .compare import compare_skeletons
from .detect import detect_blocks
from .fonts import find, render_text_in_bbox
from .skeleton import extract_skeleton
from .viz import comparison_grid, draw_bboxes


RUN_ROOT = Path(os.getenv("BANNER_RUN_ROOT", "/tmp/banner_qa_runs"))
RUN_ROOT.mkdir(parents=True, exist_ok=True)

DEFAULT_THRESHOLD = float(os.getenv("BANNER_DEFAULT_THRESHOLD", "0.30"))


banner_router = APIRouter(prefix="/api/banner", tags=["banner_qa"])


@banner_router.post("/qa")
async def banner_qa(
    image: UploadFile = File(...),
    reference_text: str = Form(...),
    family: str = Form(...),
    weight: str = Form(...),
    language: Optional[str] = Form(None),
    threshold: Optional[float] = Form(None),
) -> JSONResponse:
    """Run the local QA pipeline on one uploaded banner.

    Returns per-block compare scores + paths to PNG viz grids that can be
    fetched via `GET /api/banner/viz/{run_id}/{viz_filename}`.
    """
    font = find(family, weight)
    if font is None or not font.exists():
        raise HTTPException(400, f"unknown or missing font: {family} {weight}")

    thr = float(threshold) if threshold is not None else DEFAULT_THRESHOLD

    run_id = secrets.token_hex(8)
    run_dir = RUN_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(image.filename or "banner.png").suffix.lower() or ".png"
    banner_path = run_dir / f"banner{suffix}"
    with banner_path.open("wb") as f:
        shutil.copyfileobj(image.file, f)

    try:
        img = Image.open(banner_path).convert("RGBA")
    except Exception as e:
        raise HTTPException(400, f"cannot read uploaded image: {e}") from e

    blocks = detect_blocks(banner_path)

    overlay = draw_bboxes(img, blocks, color=(255, 32, 32))
    overlay.save(run_dir / "bboxes.png")

    block_reports = []
    overall_flagged = False
    for i, bbox in enumerate(blocks):
        crop = bbox.crop(img)
        banner_sk = extract_skeleton(crop)
        ref_img = render_text_in_bbox(reference_text, font, bbox.w, bbox.h)
        ref_sk = extract_skeleton(ref_img)
        result = compare_skeletons(banner_sk, ref_sk, threshold=thr)
        if result.flagged:
            overall_flagged = True

        grid = comparison_grid(
            crop, ref_img, banner_sk, ref_sk,
            result=result,
            title=f"block #{i}  ref={reference_text!r}",
        )
        grid_name = f"block_{i:02d}_grid.png"
        grid.save(run_dir / grid_name)

        block_reports.append({
            "idx": i,
            "bbox": [bbox.x, bbox.y, bbox.w, bbox.h],
            "compare": {
                "score": result.score,
                "iou": result.iou,
                "chamfer": result.chamfer,
                "column_sim": result.column_sim,
                "flagged": result.flagged,
            },
            "viz_path": grid_name,
        })

    response = {
        "run_id": run_id,
        "filename": image.filename,
        "reference_text": reference_text,
        "language": language,
        "font": {"family": font.family, "weight": font.weight, "variation": font.variation},
        "threshold": thr,
        "blocks": block_reports,
        "overall_status": "flag" if overall_flagged else "ok",
    }

    log_event(
        "banner_qa_run",
        run_id=run_id,
        family=font.family,
        weight=font.weight,
        language=language,
        threshold=thr,
        blocks_detected=len(blocks),
        overall_status=response["overall_status"],
    )

    return JSONResponse(response)


@banner_router.get("/viz/{run_id}/{filename}")
async def banner_viz(run_id: str, filename: str) -> FileResponse:
    """Serve a viz PNG produced by /api/banner/qa.

    Path components are validated to prevent traversal.
    """
    if not run_id.isalnum() or len(run_id) > 32:
        raise HTTPException(400, "invalid run_id")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")

    path = RUN_ROOT / run_id / filename
    if not path.is_file():
        raise HTTPException(404, "viz not found")
    return FileResponse(str(path), media_type="image/png")
