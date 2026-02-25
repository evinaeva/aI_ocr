"""
Phase 4: OCR Dispatcher.

dispatch_zone_ocr(zone, image_bytes) -> list[ZoneEngineResult]

Contract:
- If zone.engines == [] → return [] immediately (no OCR called).
- For each engine in zone.engines, call run_ocr_multi and return ZoneEngineResult list.
- Engine exceptions are caught; result has error="engine_exception".
- Never raises.
- Never logs raw OCR text.
"""
from __future__ import annotations

import logging
import time
from typing import List

from .models import ZoneDef

# Module-level import so tests can patch 'app.pipeline.ocr_dispatcher.run_ocr_multi'.
# app/ocr.py is NOT modified — we only import from it.
try:
    from app.ocr import run_ocr_multi  # type: ignore
except ImportError:  # pragma: no cover — only missing in minimal test envs
    run_ocr_multi = None  # type: ignore

logger = logging.getLogger(__name__)


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
    image_bytes: bytes,
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

    results: List[ZoneEngineResult] = []

    for engine_name in zone.engines:
        t0 = time.monotonic()
        try:
            if run_ocr_multi is None:
                raise RuntimeError("run_ocr_multi not available")

            ocr_map = run_ocr_multi(image_bytes, [engine_name])
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
