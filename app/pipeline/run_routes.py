"""
Phase 4 + Phase 5 + Phase 6: Run routes.

POST /api/templates/{template_name}/run?lang=<bcp47_or_project_lang_code>
  - Accepts: multipart/form-data with image file
  - Returns: JSON run result with additive validation block per zone

Phase 6 additions (additive):
  - Synchronous persistence to Firestore before returning response (§3)
  - Response always includes: persisted, persistence_error, persistence_error_type (§4)
  - Persistence failure → HTTP 200 with persisted=False (§5)
"""
from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

from app.logging_utils import log_event
from app.pipeline import template_store
from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr
from app.pipeline.consensus import resolve_consensus
from app.pipeline.similarity import build_validation_result, SIMILARITY_THRESHOLD
from app.pipeline.firestore_store import FIRESTORE_AVAILABLE
from app.pipeline.persistence import persist_run

run_router = APIRouter()


def _crop_zone(image_bytes: bytes, zone: ZoneDef, source_size: list) -> bytes:
    """
    Crop image to zone bbox, scaling bbox from template source_size
    to actual image pixel dimensions. Returns PNG bytes.

    No OCR text is accessed or logged here.
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
    cropped.save(buf, format="PNG")
    buf.seek(0)
    # Release image objects explicitly
    cropped.close()
    img.close()
    return buf.getvalue()


@run_router.post("/api/templates/{template_name}/run")
async def run_template(
    template_name: str,
    image: UploadFile = File(...),
    lang: str = Query(default=""),
):
    """
    Run per-zone OCR + consensus + similarity validation for all zones.

    Phase 5 (additive): adds a `validation` block to every zone in the response.
    Phase 6 (additive): persists run to Firestore synchronously; adds
      persisted / persistence_error / persistence_error_type to response.

    Returns JSON:
    {
      "run_id": "...",
      "template_name": "...",
      "persisted": true|false,
      "persistence_error": false|true,
      "persistence_error_type": null|"firestore_write_failed",
      "zones": [ ... ]
    }

    Logs: only run_id, template_name, zone_name, engine counts, rule_used,
          zone_status. Never logs OCR text.
    """
    run_id = str(uuid.uuid4())
    try:
        tmpl = template_store.get_template(template_name)
        if tmpl is None:
            raise HTTPException(status_code=404, detail="Template not found")

        image_bytes = await image.read()

        # Normalise lang: treat blank/whitespace-only as missing
        effective_lang = lang.strip() if lang else ""

        # Log metadata only — no OCR text, no image content
        log_event("run_start", run_id=run_id, template_name=template_name,
                  zone_count=len(tmpl.zones), lang=effective_lang or None)

        zone_results = []

        for zone in tmpl.zones:
            engines_configured = len(zone.engines) > 0

            if not engines_configured:
                consensus = resolve_consensus(
                    engine_results=[],
                    engines_configured=False,
                )
                # Phase 5: validation block (skip — no OCR text available)
                validation_block, _ = build_validation_result(
                    lang=effective_lang,
                    zone_name=zone.name,
                    expected_texts=getattr(tmpl, "expected_texts", None),
                    ocr_text="",
                    run_id=run_id,
                )
                log_event("zone_skip", run_id=run_id, zone_name=zone.name,
                          reason="no_engines_configured")
                zone_results.append({
                    "zone_name": zone.name,
                    "engines_used": zone.engines,
                    "engine_results": [],
                    "consensus": consensus,
                    "validation": validation_block,
                })
                continue

            # Crop zone from image; fall back to full image on error
            try:
                zone_bytes = _crop_zone(image_bytes, zone, tmpl.source_size)
            except Exception:
                zone_bytes = image_bytes

            # Dispatch per-engine OCR for this zone
            engine_results = dispatch_zone_ocr(zone, zone_bytes)

            # Resolve deterministic consensus
            consensus = resolve_consensus(
                engine_results=engine_results,
                engines_configured=True,
            )

            # Phase 5: compute validation + apply status downgrade
            selected_text = consensus.get("selected_text") or ""
            validation_block, sim_raw = build_validation_result(
                lang=effective_lang,
                zone_name=zone.name,
                expected_texts=getattr(tmpl, "expected_texts", None),
                ocr_text=selected_text,
                run_id=run_id,
            )

            # Downgrade zone_status only if it was OK and similarity < threshold
            if (
                sim_raw is not None
                and sim_raw < SIMILARITY_THRESHOLD
                and consensus.get("zone_status") == "OK"
            ):
                consensus["zone_status"] = "MANUAL"
                consensus["reason"] = "low_similarity"

            # Log metadata only — rule_used and zone_status, no OCR text
            log_event(
                "zone_result",
                run_id=run_id,
                zone_name=zone.name,
                rule_used=consensus.get("rule_used"),
                zone_status=consensus.get("zone_status"),
                reason=consensus.get("reason"),
                engines_count=len(engine_results),
            )

            zone_results.append({
                "zone_name": zone.name,
                "engines_used": zone.engines,
                "engine_results": [r.to_dict() for r in engine_results],
                "consensus": consensus,
                "validation": validation_block,
            })

        log_event("run_end", run_id=run_id, status="ok",
                  template_name=template_name, zones_processed=len(zone_results))

        response_payload = {
            "run_id": run_id,
            "template_name": template_name,
            "zones": zone_results,
        }

        # ── Phase 6: synchronous persistence (§3) ────────────────────────────
        if FIRESTORE_AVAILABLE:
            persistence_flags = persist_run(
                run_id=run_id,
                template_name=template_name,
                lang=effective_lang or None,
                zones=zone_results,
            )
            response_payload.update(persistence_flags)

        return JSONResponse(response_payload)

    except HTTPException:
        raise
    except Exception:
        log_event("run_end", run_id=run_id, status="error",
                  template_name=template_name)
        return JSONResponse({"error": "internal_error"}, status_code=500)
