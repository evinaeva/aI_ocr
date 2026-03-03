"""
Phase 4 + Phase 5 + Phase 6: Run routes.

POST /api/templates/{template_name}/run?lang=<bcp47_or_project_lang_code>
  - Accepts: multipart/form-data with image file
  - Returns: JSON run result with additive validation block per zone

Phase 6 additions (additive):
  - Synchronous persistence to Firestore before returning response (§3)
  - Response always includes: persisted, persistence_error, persistence_error_type (§4)
  - Persistence failure → HTTP 200 with persisted=False (§5)

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
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import uuid

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image
import cv2
import numpy as np

from app.logging_utils import log_event
from app.pipeline import template_store
from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr
from app.pipeline.ocr_dispatcher import ZoneEngineResult
from app.pipeline.consensus import resolve_consensus
from app.pipeline.similarity import build_validation_result, SIMILARITY_THRESHOLD
from app.pipeline.firestore_store import is_persistence_enabled
from app.pipeline.persistence import persist_run
from app.pipeline.logo_matcher import match_logo_zone
from app.pipeline.preprocessor import load_image, maybe_upscale, scale_bbox, crop_zone_to_png
from app.ocr import (
    google_batch_annotate_images,
    _google_cache_put,
    _google_cache_clear,
)

run_router = APIRouter()
logger = logging.getLogger(__name__)


def _crop_zone(image_bytes: bytes, zone: ZoneDef, source_size: list) -> bytes:
    """
    Crop image to zone bbox, scaling bbox from template source_size
    to current image dimensions (after optional whole-image upscale).
    Returns PNG bytes.
    """
    img = load_image(image_bytes)
    img, _ = maybe_upscale(img)

    try:
        src_w = int(source_size[0])
        src_h = int(source_size[1])
    except Exception:
        src_w, src_h = img.width, img.height

    bbox_scaled = scale_bbox(zone.bbox, [max(1, src_w), max(1, src_h)], [img.width, img.height])
    return _guard_png_size(crop_zone_to_png(img, bbox_scaled))


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
    """
    Backward-compat shim: delegates to logo_matcher.match_logo_zone().

    Run OpenCV template matching for logo zones. Supports a list of base64-encoded
    logo templates via logo_templates_base64 in zone.engine_config. A single
    backward-compatible key logo_template_base64 is also honored. Templates stored
    in GCS are loaded via logo_gcs_set_key (or the "default" set when the
    LOGO_TEMPLATES_GCS_BUCKET env var is set).

    For each template, multi-scale TM_CCOEFF_NORMED matching in grayscale is used.
    The highest score across all (template x scale) combinations is compared against
    a configurable threshold (default 0.7 from LOGO_MATCH_THRESHOLD env var).
    score >= threshold -> OK, else -> MANUAL. OCR engines are never invoked.
    """
    return match_logo_zone(zone_bytes, zone.engine_config or {})


def _prefetch_google_batch(
    image_bytes: bytes,
    zones: list,
    source_size: list,
) -> dict:
    """
    Crop images for all zones that include 'google' in their engines.
    Calls google_batch_annotate_images in chunks of 16.
    Injects results into the OCR module cache via _google_cache_put.

    Returns a dict {zone_index: zone_bytes} for zones that were batched,
    so run_routes can pass the SAME bytes object to dispatch_zone_ocr
    (object-identity match required for cache lookup).

    Never raises.
    """
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
        job_bytes = [zb for _, zb in jobs]
        try:
            batch_results = google_batch_annotate_images(job_bytes, google_mode=google_mode)
        except Exception:
            logger.warning("google_batch_prefetch_failed mode=%s", google_mode)
            continue

        while len(batch_results) < len(jobs):
            batch_results.append(OCRResult("", 0.0, "google"))

        for (zone_idx, zb), result in zip(jobs, batch_results):
            _google_cache_put(zb, result)
            zone_bytes_map[zone_idx] = zb

    return zone_bytes_map


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
      These three keys are ALWAYS present, even when persistence is disabled.
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

        # Google-batch-v2 prefetch
        batched_zone_bytes = await asyncio.to_thread(
            _prefetch_google_batch,
            image_bytes,
            tmpl.zones,
            tmpl.source_size,
        )

        zone_results = []

        for i, zone in enumerate(tmpl.zones):
            engines_configured = len(zone.engines) > 0

            # Logo zones have no OCR engines by design; skip the no-engines guard.
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

            # Use same bytes object if it was batched (logo zones are not batched).
            if i in batched_zone_bytes:
                zone_bytes = batched_zone_bytes[i]
            else:
                try:
                    zone_bytes = _crop_zone(image_bytes, zone, tmpl.source_size)
                except Exception:
                    zone_bytes = None

            if zone.type == "logo":
                # Logo branch: OpenCV only, OCR dispatcher is never called.
                engine_results = []
                consensus = _logo_template_match(zone_bytes, zone)
                log_event(
                    "zone_logo_match",
                    run_id=run_id,
                    zone_name=zone.name,
                    zone_status=consensus.get("zone_status"),
                    logo_score=consensus.get("logo_score"),
                    reason=consensus.get("reason"),
                )
            else:
                if zone_bytes is None:
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
                    crop_width, crop_height = _png_dimensions(zone_bytes)
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
                        dispatch_zone_ocr, zone, zone_bytes
                    )

                consensus = resolve_consensus(
                    engine_results=engine_results,
                    engines_configured=True,
                )

            selected_text = consensus.get("selected_text") or ""

            validation_block, sim_raw = build_validation_result(
                lang=effective_lang,
                zone_name=zone.name,
                expected_texts=getattr(tmpl, "expected_texts", None),
                ocr_text=selected_text,
                run_id=run_id,
            )

            # Low-similarity downgrade applies only to OCR zones, not logo zones.
            if (
                sim_raw is not None
                and sim_raw < SIMILARITY_THRESHOLD
                and consensus.get("zone_status") == "OK"
                and zone.type != "logo"
            ):
                consensus["zone_status"] = "MANUAL"
                consensus["reason"] = "low_similarity"

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

        response_payload = {
            "run_id": run_id,
            "template_name": template_name,
            "zones": zone_results,
        }

        if is_persistence_enabled():
            persistence_flags = persist_run(
                run_id=run_id,
                template_name=template_name,
                lang=effective_lang or None,
                zones=zone_results,
            )
            response_payload.update(persistence_flags)
        else:
            response_payload.update(
                {
                    "persisted": False,
                    "persistence_error": False,
                    "persistence_error_type": None,
                }
            )

        return JSONResponse(response_payload)

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
            _google_cache_clear([id(zb) for zb in batched_zone_bytes.values()])
