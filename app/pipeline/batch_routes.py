"""
P2.4 — v2-batch job orchestration.

Endpoints (all additive, no legacy changes):
  POST /api/v2/batch/start
      Body: multipart — zip_file, template_name, lang (optional)
      Returns: {job_id, status, target_count}

  GET  /api/v2/batch/{job_id}/progress
      SSE stream with events: start, target_start, zone_result, target_done, done, error, ping

  GET  /api/v2/batch/{job_id}/results
      Returns aggregated results for completed/error job.

Worker mechanism: asyncio.create_task (same as legacy /api/upload in main.py).
NO Pub/Sub — Pub/Sub (ocr-jobs topic) belongs to the separate ocr-checker system,
not to ai-ocr.

Google batching: ≤16 crops per batch_annotate_images call, implemented via
_prefetch_google_batch (imported from run_routes) before each target's zone loop.

Progress: uses module-level _batch_sse_queues (asyncio.Queue per job_id), separate
from main.py's _sse_queues to avoid key collisions.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time
import uuid
from typing import AsyncGenerator, Dict, Optional
from zipfile import ZipFile

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from PIL import Image

from app.logging_utils import log_event
from app.pipeline import template_store
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr
from app.pipeline.consensus import resolve_consensus
from app.pipeline.similarity import build_validation_result, SIMILARITY_THRESHOLD
from app.zip_processor import build_zip_manifest
from app.ocr import (
    google_batch_annotate_images,
    _google_cache_put,
    _google_cache_clear,
    OCRResult,
)

batch_router = APIRouter()
logger = logging.getLogger(__name__)

# SSE queues: job_id -> asyncio.Queue
_batch_sse_queues: Dict[str, asyncio.Queue] = {}

# In-memory job store: job_id -> dict
# Persisted to _JOBS dict for the lifetime of this process instance.
# (Cloud Run may route to a different instance for /results — acceptable for
# a v2 batch MVP; Firestore persistence is a future enhancement.)
_JOBS: Dict[str, dict] = {}


def _push_batch_event(job_id: str, event: dict) -> None:
    """Push an event to the SSE queue for job_id. Non-blocking."""
    q = _batch_sse_queues.get(job_id)
    if q:
        try:
            q.put_nowait(json.dumps(event))
        except asyncio.QueueFull:
            pass


def _crop_zone_bytes(image_bytes: bytes, zone, source_size: list) -> bytes:
    """
    Crop image to zone bbox, scaling from template source_size to actual image size.
    Upscales whole image to at least 1024x768 if needed (per spec).
    Returns PNG bytes. Never raises (falls back to full image).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img_w, img_h = img.size

        if img_w < 1024 or img_h < 768:
            scale = max(1024 / max(1, img_w), 768 / max(1, img_h))
            new_w = max(1, int(round(img_w * scale)))
            new_h = max(1, int(round(img_h * scale)))
            img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            img_w, img_h = img.size

        try:
            src_w = max(1, int(source_size[0]))
            src_h = max(1, int(source_size[1]))
        except Exception:
            src_w, src_h = img_w, img_h

        scale_x = img_w / src_w
        scale_y = img_h / src_h

        x1, y1, x2, y2 = zone.bbox
        px1 = max(0, min(int(x1 * scale_x), img_w))
        py1 = max(0, min(int(y1 * scale_y), img_h))
        px2 = max(0, min(int(x2 * scale_x), img_w))
        py2 = max(0, min(int(y2 * scale_y), img_h))

        cropped = img.crop((px1, py1, px2, py2))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        buf.seek(0)
        cropped.close()
        img.close()
        return buf.getvalue()
    except Exception:
        return image_bytes


def _prefetch_google_for_target(
    image_bytes: bytes,
    zones: list,
    source_size: list,
) -> dict:
    """
    Batch-prefetch Google Vision for all zones in a single target image.
    Chunks at ≤16 per batch_annotate_images call.
    Injects results into the OCR module cache.
    Returns {zone_index: zone_bytes} for cache-hit zones.
    Never raises.
    """
    google_jobs_by_mode: dict = {}

    for i, zone in enumerate(zones):
        if "google" not in zone.engines:
            continue
        zb = _crop_zone_bytes(image_bytes, zone, source_size)
        google_mode = (zone.engine_config or {}).get("google_mode")
        mode_key = (google_mode or "text").strip().lower()
        google_jobs_by_mode.setdefault(mode_key, []).append((i, zb))

    if not google_jobs_by_mode:
        return {}

    zone_bytes_map: dict = {}

    for google_mode, jobs in google_jobs_by_mode.items():
        job_bytes = [zb for _, zb in jobs]
        try:
            batch_results = google_batch_annotate_images(job_bytes, google_mode=google_mode)
        except Exception:
            logger.warning(
                "batch_routes: google_batch_prefetch_failed mode=%s", google_mode
            )
            continue

        while len(batch_results) < len(jobs):
            batch_results.append(OCRResult("", 0.0, "google"))

        for (zone_idx, zb), result in zip(jobs, batch_results):
            _google_cache_put(zb, result)
            zone_bytes_map[zone_idx] = zb

    return zone_bytes_map


def _run_zones_for_target(
    job_id: str,
    target_id: str,
    lang: str,
    image_bytes: bytes,
    tmpl,
) -> list:
    """
    Synchronous: crop + OCR + consensus + validation for all zones on one image.
    Returns list of zone_result dicts (same schema as run_routes).
    This runs in asyncio.to_thread.
    """
    run_id = job_id  # use job_id as run_id for logging
    zones = tmpl.zones
    source_size = tmpl.source_size

    # Google batch prefetch (≤16 per call)
    batched_zone_bytes = _prefetch_google_for_target(image_bytes, zones, source_size)

    zone_results = []
    try:
        for i, zone in enumerate(zones):
            if not zone.engines:
                consensus = resolve_consensus(engine_results=[], engines_configured=False)
                validation_block, _ = build_validation_result(
                    lang=lang,
                    zone_name=zone.name,
                    expected_texts=getattr(tmpl, "expected_texts", None),
                    ocr_text="",
                    run_id=run_id,
                )
                zone_results.append({
                    "zone_name": zone.name,
                    "engines_used": zone.engines,
                    "engine_results": [],
                    "consensus": consensus,
                    "validation": validation_block,
                })
                continue

            zone_bytes = batched_zone_bytes.get(i) or _crop_zone_bytes(image_bytes, zone, source_size)

            engine_results = dispatch_zone_ocr(zone, zone_bytes)

            consensus = resolve_consensus(
                engine_results=engine_results,
                engines_configured=True,
            )

            selected_text = consensus.get("selected_text") or ""
            validation_block, sim_raw = build_validation_result(
                lang=lang,
                zone_name=zone.name,
                expected_texts=getattr(tmpl, "expected_texts", None),
                ocr_text=selected_text,
                run_id=run_id,
            )

            if (
                sim_raw is not None
                and sim_raw < SIMILARITY_THRESHOLD
                and consensus.get("zone_status") == "OK"
            ):
                consensus["zone_status"] = "MANUAL"
                consensus["reason"] = "low_similarity"

            zone_results.append({
                "zone_name": zone.name,
                "engines_used": zone.engines,
                "engine_results": [r.to_dict() for r in engine_results],
                "consensus": consensus,
                "validation": validation_block,
            })
    finally:
        # Clean up Google cache entries
        if batched_zone_bytes:
            _google_cache_clear([id(zb) for zb in batched_zone_bytes.values()])

    return zone_results


async def _process_batch_job(
    job_id: str,
    zip_bytes: bytes,
    template_name: str,
    lang_hint: str,
) -> None:
    """
    Background asyncio task: processes all targets in the ZIP.
    Pushes SSE events for progress. Stores results in _JOBS.
    """
    _JOBS[job_id]["status"] = "processing"
    log_event("batch_job_start", run_id=job_id, template_name=template_name)

    try:
        tmpl = template_store.get_template(template_name)
        if tmpl is None:
            raise ValueError(f"Template '{template_name}' not found")

        # Build manifest from ZIP
        manifest = await asyncio.to_thread(build_zip_manifest, zip_bytes)

        targets = manifest  # list of ZipTargetManifest
        total = len(targets)
        _JOBS[job_id]["total"] = total

        _push_batch_event(job_id, {"event": "start", "job_id": job_id, "total": total})

        results = []
        manual_count = ok_count = error_count = 0

        # Open ZIP once for all targets; read image bytes via item.archive_path
        with ZipFile(io.BytesIO(zip_bytes)) as zf:
            for idx, target in enumerate(targets):
                target_id = target.target_id

                # Build lang -> image_bytes from manifest items (deterministic: sorted by lang)
                images_by_lang: Dict[str, bytes] = {}
                for item in sorted(target.items, key=lambda it: (it.lang or "")):
                    if item.lang:
                        try:
                            images_by_lang[item.lang] = zf.read(item.archive_path)
                        except Exception:
                            pass

                # Determine language for this target
                lang = lang_hint
                if not lang:
                    # Use EN if available (has_en flag), else first lang in items
                    if target.has_en:
                        lang = "en"
                    elif images_by_lang:
                        lang = sorted(images_by_lang.keys())[0]

                _push_batch_event(job_id, {
                    "event": "target_start",
                    "idx": idx,
                    "target_id": target_id,
                    "lang": lang,
                    "image_count": len(images_by_lang),
                })

                # Process the image for this target+lang
                img_bytes = None
                if lang and lang in images_by_lang:
                    img_bytes = images_by_lang[lang]
                elif images_by_lang:
                    # Fallback to first available language image (deterministic)
                    img_bytes = images_by_lang[sorted(images_by_lang.keys())[0]]

                if img_bytes is None:
                    error_count += 1
                    results.append({
                        "target_id": target_id,
                        "lang": lang,
                        "status": "error",
                        "reason": "no_image",
                        "zones": [],
                    })
                    _push_batch_event(job_id, {
                        "event": "target_done",
                        "idx": idx,
                        "target_id": target_id,
                        "status": "error",
                        "reason": "no_image",
                    })
                    continue

                try:
                    zone_results = await asyncio.to_thread(
                        _run_zones_for_target,
                        job_id, target_id, lang, img_bytes, tmpl,
                    )

                    # Determine overall target status
                    statuses = [z["consensus"].get("zone_status") for z in zone_results]
                    if "MANUAL" in statuses:
                        target_status = "MANUAL"
                        manual_count += 1
                    else:
                        target_status = "OK"
                        ok_count += 1

                    results.append({
                        "target_id": target_id,
                        "lang": lang,
                        "status": target_status,
                        "zones": zone_results,
                    })

                    log_event(
                        "batch_target_done",
                        run_id=job_id,
                        target_id=target_id,
                        lang=lang,
                        zone_count=len(zone_results),
                        target_status=target_status,
                    )

                    _push_batch_event(job_id, {
                        "event": "target_done",
                        "idx": idx,
                        "target_id": target_id,
                        "lang": lang,
                        "status": target_status,
                        "zone_count": len(zone_results),
                    })

                except Exception as exc:
                    error_count += 1
                    logger.warning(
                        "batch_routes: target_failed job_id=%s target_id=%s: %s",
                        job_id, target_id, type(exc).__name__,
                    )
                    results.append({
                        "target_id": target_id,
                        "lang": lang,
                        "status": "error",
                        "reason": "processing_error",
                        "zones": [],
                    })
                    _push_batch_event(job_id, {
                        "event": "target_done",
                        "idx": idx,
                        "target_id": target_id,
                        "status": "error",
                    })

        _JOBS[job_id].update({
            "status": "done",
            "results": results,
            "ok_count": ok_count,
            "manual_count": manual_count,
            "error_count": error_count,
            "finished_at": time.time(),
        })

        log_event(
            "batch_job_done",
            run_id=job_id,
            template_name=template_name,
            total=total,
            ok=ok_count,
            manual=manual_count,
            error=error_count,
        )

        _push_batch_event(job_id, {
            "event": "done",
            "job_id": job_id,
            "ok": ok_count,
            "manual": manual_count,
            "error": error_count,
        })

    except Exception as exc:
        logger.exception("batch_routes: job_failed job_id=%s", job_id)
        _JOBS[job_id].update({
            "status": "error",
            "error_message": type(exc).__name__,
            "finished_at": time.time(),
        })
        log_event("batch_job_error", run_id=job_id, error_type=type(exc).__name__)
        _push_batch_event(job_id, {"event": "error", "job_id": job_id, "message": type(exc).__name__})


@batch_router.post("/api/v2/batch/start")
async def batch_start(
    zip_file: UploadFile = File(...),
    template_name: str = Form(...),
    lang: Optional[str] = Form(default=""),
):
    """
    Start a v2 batch job.
    Accepts a ZIP archive and a template name.
    Returns immediately with job_id; processing continues in background.
    """
    tmpl = template_store.get_template(template_name)
    if tmpl is None:
        return JSONResponse(
            {"error": "template_not_found", "template_name": template_name},
            status_code=404,
        )

    job_id = str(uuid.uuid4())
    zip_bytes = await zip_file.read()
    lang_hint = (lang or "").strip()

    _JOBS[job_id] = {
        "job_id": job_id,
        "template_name": template_name,
        "lang": lang_hint or None,
        "status": "pending",
        "total": None,
        "results": None,
        "created_at": time.time(),
    }

    log_event(
        "batch_job_enqueued",
        run_id=job_id,
        template_name=template_name,
        lang=lang_hint or None,
    )

    asyncio.create_task(
        _process_batch_job(job_id, zip_bytes, template_name, lang_hint)
    )

    return JSONResponse({
        "job_id": job_id,
        "status": "pending",
        "template_name": template_name,
        "progress_url": f"/api/v2/batch/{job_id}/progress",
        "results_url": f"/api/v2/batch/{job_id}/results",
    })


@batch_router.get("/api/v2/batch/{job_id}/progress")
async def batch_progress(job_id: str):
    """
    SSE stream for batch job progress.
    Events: start, target_start, target_done, done, error, ping.
    Closes after done/error event or 30s idle timeout (ping sent).
    """
    if job_id not in _JOBS:
        return JSONResponse({"error": "job_not_found"}, status_code=404)

    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _batch_sse_queues[job_id] = q

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {msg}\n\n"
                    data = json.loads(msg)
                    if data.get("event") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield 'data: {"event": "ping"}\n\n'
                    # Check if job already finished (client reconnected after done)
                    job = _JOBS.get(job_id)
                    if job and job.get("status") in ("done", "error"):
                        break
        finally:
            _batch_sse_queues.pop(job_id, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@batch_router.get("/api/v2/batch/{job_id}/results")
async def batch_results(job_id: str):
    """
    Return aggregated results for a completed batch job.
    Returns 404 for unknown jobs, 202 for in-progress jobs.
    """
    job = _JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "job_not_found"}, status_code=404)

    status = job.get("status")
    if status in ("pending", "processing"):
        return JSONResponse(
            {"job_id": job_id, "status": status, "message": "job_in_progress"},
            status_code=202,
        )

    return JSONResponse({
        "job_id": job_id,
        "template_name": job.get("template_name"),
        "lang": job.get("lang"),
        "status": status,
        "total": job.get("total"),
        "ok_count": job.get("ok_count", 0),
        "manual_count": job.get("manual_count", 0),
        "error_count": job.get("error_count", 0),
        "results": job.get("results") or [],
        "error_message": job.get("error_message"),
    })
