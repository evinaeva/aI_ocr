"""
Phase 4: OCR Dispatcher.

dispatch_zone_ocr(zone, cropped_image) -> list[ZoneEngineResult]

Contract:
- If zone.engines == [] → return [] immediately (no OCR called).
- For each engine in zone.engines, call run_ocr_multi and return ZoneEngineResult list.
- Engine exceptions are caught; result has error="engine_exception".
- Never raises.
- Never logs raw OCR text.
"""
from __future__ import annotations

import logging
import os
import time
from typing import List

from .cropped_image import CroppedImage
from .models import ZoneDef

# Module-level import so tests can patch 'app.pipeline.ocr_dispatcher.run_ocr_multi'.
# app/ocr.py is NOT modified — we only import from it.
try:
    from app.ocr import run_ocr_multi  # type: ignore
except ImportError:  # pragma: no cover — only missing in minimal test envs
    run_ocr_multi = None  # type: ignore

logger = logging.getLogger(__name__)


def _measure_payload(engine_name: str, image_bytes: bytes) -> bytes:
    payload_size = len(image_bytes)
    threshold_raw = os.getenv(f"OCR_MAX_PAYLOAD_BYTES_{engine_name.upper()}", "").strip()
    threshold = int(threshold_raw) if threshold_raw.isdigit() else None

    if threshold is not None and payload_size > threshold:
        logger.warning(
            '{"event":"ocr_payload_too_large","engine":"%s","payload_bytes":%d,'
            '"threshold_bytes":%d,"fallback":"not_applied"}',
            engine_name,
            payload_size,
            threshold,
        )
    else:
        logger.info(
            '{"event":"ocr_payload_measured","engine":"%s","payload_bytes":%d,"threshold_bytes":%s}',
            engine_name,
            payload_size,
            "null" if threshold is None else str(threshold),
        )

    return image_bytes


class ZoneEngineResult:
    """Single engine result for a single zone."""

    def __init__(
        self,
        engine: str,
        text: str,
        confidence,   # float | None
        latency_ms: float,
        error,        # str | None
    ):
        self.engine = engine
        self.text = text
        self.confidence = confidence
        self.latency_ms = latency_ms
        self.error = error

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "text": self.text,
            "confidence": self.confidence,
            "latency_ms": round(self.latency_ms, 1),
            "error": self.error,
        }


def dispatch_zone_ocr(
    zone: ZoneDef,
    cropped_image: CroppedImage,
) -> List[ZoneEngineResult]:
    """
    Run OCR for every engine configured in zone.engines.

    Returns list[ZoneEngineResult].
    Returns [] immediately if zone.engines is empty.
    Never raises.
    Never logs raw OCR text — only engine name and error code.
    """
    if not zone.engines:
        return []
    if not isinstance(cropped_image, CroppedImage):
        raise RuntimeError("dispatch_zone_ocr requires CroppedImage payload")
    if not isinstance(cropped_image.original_sha256, str) or not cropped_image.original_sha256.strip():
        raise RuntimeError("dispatch_zone_ocr requires crop payload with original hash")
    cropped_image.validate_for_ocr()

    results: List[ZoneEngineResult] = []
    zone_bytes = cropped_image.bytes

    for engine_name in zone.engines:
        t0 = time.monotonic()
        try:
            if run_ocr_multi is None:
                raise RuntimeError("run_ocr_multi not available")

            measured_bytes = _measure_payload(engine_name, zone_bytes)
            ocr_map = run_ocr_multi(measured_bytes, [engine_name], zone.engine_config)
            elapsed = (time.monotonic() - t0) * 1000.0

            if engine_name in ocr_map:
                r = ocr_map[engine_name]
                # Normalise confidence: treat 0.0 on empty text as None
                conf = r.confidence
                if conf == 0.0 and not r.text:
                    conf = None
                results.append(
                    ZoneEngineResult(
                        engine=engine_name,
                        text=r.text,
                        confidence=conf,
                        latency_ms=elapsed,
                        error=None,
                    )
                )
            else:
                elapsed = (time.monotonic() - t0) * 1000.0
                # Log metadata only — no OCR text
                logger.warning(
                    "dispatch_zone_ocr: engine=%s no_result", engine_name
                )
                results.append(
                    ZoneEngineResult(
                        engine=engine_name,
                        text="",
                        confidence=None,
                        latency_ms=elapsed,
                        error="engine_no_result",
                    )
                )
        except Exception as exc:
            elapsed = (time.monotonic() - t0) * 1000.0
            # Log metadata only — exc type/message may contain OCR text in theory,
            # so we log only the exception type string, not the full message.
            logger.warning(
                "dispatch_zone_ocr: engine=%s exception_type=%s",
                engine_name,
                type(exc).__name__,
            )
            results.append(
                ZoneEngineResult(
                    engine=engine_name,
                    text="",
                    confidence=None,
                    latency_ms=elapsed,
                    error="engine_exception",
                )
            )

    return results
