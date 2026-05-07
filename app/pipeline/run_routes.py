"""
Phase 4 + Phase 5: Run routes.

POST /api/templates/{template_name}/run?lang=<bcp47_or_project_lang_code>
  - Accepts: multipart/form-data with image file
  - Returns: JSON run result with one zone-result entry per template zone,
             including consensus and the Phase 5 validation block.

A2 fix: dispatch_zone_ocr is a sync function; called via asyncio.to_thread
  to avoid blocking the event loop.

Google-batch-v2 (additive):
  Before processing zones, all zones with Google engine are batched via
  google_batch_annotate_images (up to 16 per call). Results are injected into
  the per-image cache in app/ocr so _ocr_google consumes them without an
  extra API call. Dispatcher, done++ and semaphore are completely unchanged.

Logo-GCS (additive):
  Logo zones are handled exclusively by logo_matcher.match_logo_zone().
  OCR engines are never invoked for logo zones.
  _logo_template_match is kept as a thin backward-compat shim that delegates
  to logo_matcher.

PASS / MANUAL decision (Phase 5):
  An OK zone is downgraded to MANUAL with reason `no_text_match`
  whenever the validation block reports `match_pass == False`. The PASS
  primitive is line-order-insensitive but character-strict within each
  line; see app.normalizer.compare_lines for the exact contract.

No persistence:
  Run results are not written to Firestore. The response is the only
  consumer; once the operator has reviewed PASS/MANUAL the data is
  intentionally discarded.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

from app.logging_utils import log_event
from app.pipeline import template_store
from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr
from app.pipeline.ocr_dispatcher import ZoneEngineResult
from app.pipeline.consensus import resolve_consensus
from app.pipeline.similarity import build_validation_result
from app.pipeline.logo_matcher import match_logo_zone
from app.pipeline.cropped_image import CroppedImage
from app.pipeline.preprocessor import load_image, make_cropped_image, maybe_upscale, scale_bbox, crop_zone_to_png
from app.ocr import (
    google_batch_annotate_images,
    _google_cache_put,
    _google_cache_clear,
)

run_router = APIRouter()
logger = logging.getLogger(__name__)


def _crop_zone(image_bytes: bytes, zone: ZoneDef, source_size: list) -> CroppedImage:
    """
    Crop image to zone bbox, scaling bbox from template source_size
    to current image dimensions (after optional whole-image upscale).
    Returns validated CroppedImage.
    """
    img = load_image(image_bytes)
    img, _ = maybe_upscale(img)

    try:
        src_w = int(source_size[0])
        src_h = int(source_size[1])
    except Exception:
        src_w, src_h = img.width, img.height

    bbox_scaled = scale_bbox(zone.bbox, [max(1, src_w), max(1, src_h)], [img.width, img.height])
    cropped_bytes = _guard_png_size(crop_zone_to_png(img, bbox_scaled))
    return make_cropped_image(
        image_bytes,
        bbox_scaled,
        cropped_bytes,
        original_width=img.width,
        original_height=img.height,
        crop_width=max(1, bbox_scaled[2] - bbox_scaled[0]),
        crop_height=max(1, bbox_scaled[3] - bbox_scaled[1]),
    )


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(payload)) as img:
        return img.width, img.height


def _guard_png_size(payload: bytes) -> bytes:
    """Deterministic payload-size guard for OCR requests."""
    max_bytes_raw = os.getenv("OCR_MAX_PAYLOAD_BYTES", "4194304").strip()
    max_bytes = int(max_bytes_raw) if max_bytes_raw.isdigit() else 4194304
    if len(payload) <= max_bytes:
        return payload

    img = Image.open(io.BytesIO(payload))
    try:
        while len(payload) > max_bytes and img.width > 64 and img.height > 64:
            new_w = max(64, int(round(img.width * 0.9)))
            new_h = max(64, int(round(img.height * 0.9)))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            payload = buf.getvalue()
    finally:
        img.close()
    return payload


def _logo_template_match(zone_bytes: bytes, zone: ZoneDef) -> dict:
    """Backward-compat shim: delegates to logo_matcher.match_logo_zone()."""
    return match_logo_zone(zone_bytes, zone.engine_config or {})


def _prefetch_google_batch(
    image_bytes: bytes,
    zones: list,
    source_size: list,
) -> dict:
    """Batched Google OCR prefetch (see module docstring)."""
    google_jobs_by_mode = {}

    for i, zone in enumerate(zones):
        if zone.type == "logo" or "google" not in zone.engines:
            continue
        try:
            zb = _crop_zone(image_bytes, zone, source_size)
        except Exception:
            log_event(
                "zone_crop_failed",
                zone_index=i,
                zone_name=zone.name,
                engine="google",
                source="crop",
            )
            continue

        google_mode = (zone.engine_config or {}).get("google_mode")
        mode_key = (google_mode or "text").strip().lower()
        google_jobs_by_mode.setdefault(mode_key, []).append((i, zb))

    if not google_jobs_by_mode:
        return {}

    zone_bytes_map = {}
    from app.ocr import OCRResult

    for google_mode, jobs in google_jobs_by_mode.items():
        job_bytes = [cropped.bytes for _, cropped in jobs]
        try:
            batch_results = google_batch_annotate_images(job_bytes, google_mode=google_mode)
        except Exception:
            logger.warning("google_batch_prefetch_failed mode=%s", google_mode)
            continue

        while len(batch_results) < len(jobs):
            batch_results.append(OCRResult("", 0.0, "google"))

        for (zone_idx, cropped), result in zip(jobs, batch_results):
            _google_cache_put(cropped.bytes, result)
            zone_bytes_map[zone_idx] = cropped

    return zone_bytes_map


@run_router.post("/api/templates/{template_name}/run")
async def run_template(
    template_name: str,
    image: UploadFile = File(...),
    lang: str = Query(default=""),
):
    """
    Run per-zone OCR + consensus + line-order-insensitive PASS validation.

    Returns a JSON object with `run_id`, `template_name`, and a `zones`
    list. Each zone entry contains `consensus` and `validation` blocks.
    Run results are not persisted.
    """
    run_id = str(uuid.uuid4())
    batched_zone_bytes: dict = {}

    try:
        tmpl = template_store.get_template(template_name)
        if tmpl is None:
            raise HTTPException(status_code=404, detail="Template not found")

        image_bytes = await image.read()
        effective_lang = lang.strip() if lang else ""

        log_event(
            "run_start",
            run_id=run_id,
            template_name=template_name,
            zone_count=len(tmpl.zones),
            lang=effective_lang or None,
        )

        batched_zone_bytes = await asyncio.to_thread(
            _prefetch_google_batch,
            image_bytes,
            tmpl.zones,
            tmpl.source_size,
        )

        zone_results = []

        for i, zone in enumerate(tmpl.zones):
            engines_configured = len(zone.engines) > 0

            if not engines_configured and zone.type != "logo":
                consensus = resolve_consensus(
                    engine_results=[],
                    engines_configured=False,
                )
                validation_block, _ = build_validation_result(
                    lang=effective_lang,
                    zone_name=zone.name,
                    expected_texts=getattr(tmpl, "expected_texts", None),
                    ocr_text="",
                    run_id=run_id,
                )

                log_event(
                    "zone_skip",
                    run_id=run_id,
                    zone_name=zone.name,
                    reason="no_engines_configured",
                )

                zone_results.append(
                    {
                        "zone_name": zone.name,
                        "engines_used": zone.engines,
                        "engine_results": [],
                        "consensus": consensus,
                        "validation": validation_block,
                    }
                )
                continue

            if i in batched_zone_bytes:
                cropped_image = batched_zone_bytes[i]
            else:
                try:
                    cropped_image = _crop_zone(image_bytes, zone, tmpl.source_size)
                except Exception:
                    cropped_image = None

            if zone.type == "logo":
                engine_results = []
                logo_bytes = cropped_image.bytes if cropped_image is not None else b""
                consensus = _logo_template_match(logo_bytes, zone)
                log_event(
                    "zone_logo_match",
                    run_id=run_id,
                    zone_name=zone.name,
                    zone_status=consensus.get("zone_status"),
                    logo_score=consensus.get("logo_score"),
                    reason=consensus.get("reason"),
                )
            else:
                if cropped_image is None:
                    log_event(
                        "zone_crop_failed",
                        run_id=run_id,
                        zone_index=i,
                        zone_name=zone.name,
                        source="crop",
                    )
                    engine_results = [
                        ZoneEngineResult(
                            engine=engine_name,
                            text="",
                            confidence=None,
                            latency_ms=0.0,
                            error="engine_exception",
                        )
                        for engine_name in zone.engines
                    ]
                else:
                    crop_width, crop_height = _png_dimensions(cropped_image.bytes)
                    for engine_name in zone.engines:
                        log_event(
                            "ocr_payload",
                            run_id=run_id,
                            zone_index=i,
                            zone_name=zone.name,
                            engine=engine_name,
                            source="crop",
                            crop_width=crop_width,
                            crop_height=crop_height,
                        )

                    engine_results = await asyncio.to_thread(
                        dispatch_zone_ocr, zone, cropped_image
                    )

                consensus = resolve_consensus(
                    engine_results=engine_results,
                    engines_configured=True,
                )

            selected_text = consensus.get("selected_text") or ""

            validation_block, _sim = build_validation_result(
                lang=effective_lang,
                zone_name=zone.name,
                expected_texts=getattr(tmpl, "expected_texts", None),
                ocr_text=selected_text,
                run_id=run_id,
            )

            # PASS / MANUAL: downgrade only when validation actually ran.
            # Logo zones don't go through expected-text validation.
            if (
                validation_block.get("validation_applied") is True
                and validation_block.get("match_pass") is False
                and consensus.get("zone_status") == "OK"
                and zone.type != "logo"
            ):
                consensus["zone_status"] = "MANUAL"
                consensus["reason"] = "no_text_match"

            log_event(
                "zone_result",
                run_id=run_id,
                zone_name=zone.name,
                rule_used=consensus.get("rule_used"),
                zone_status=consensus.get("zone_status"),
                reason=consensus.get("reason"),
                engines_count=len(engine_results),
            )

            zone_results.append(
                {
                    "zone_name": zone.name,
                    "engines_used": zone.engines,
                    "engine_results": [r.to_dict() for r in engine_results],
                    "consensus": consensus,
                    "validation": validation_block,
                }
            )

        log_event(
            "run_end",
            run_id=run_id,
            status="ok",
            template_name=template_name,
            zones_processed=len(zone_results),
        )

        return JSONResponse({
            "run_id": run_id,
            "template_name": template_name,
            "zones": zone_results,
        })

    except HTTPException:
        raise
    except Exception:
        log_event(
            "run_end",
            run_id=run_id,
            status="error",
            template_name=template_name,
        )
        return JSONResponse({"error": "internal_error"}, status_code=500)

    finally:
        if batched_zone_bytes:
            _google_cache_clear([id(cropped.bytes) for cropped in batched_zone_bytes.values()])
