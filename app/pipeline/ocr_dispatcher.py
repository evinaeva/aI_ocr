"""
Phase 4: OCR Dispatcher.

dispatch_zone_ocr(zone, image_bytes) -> list[ZoneEngineResult]

Contract:
- If zone.engines == [] → return [] immediately (no OCR called).
- For each engine in zone.engines, call run_ocr_multi and return ZoneEngineResult list.
- Engine exceptions are caught; result has error="engine_exception".
- Never raises.
"""
from __future__ import annotations

import logging
import time
from typing import List

from .models import ZoneDef

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
    """
    if not zone.engines:
        return []

    # Import here to avoid circular imports; ocr.py must not be modified.
    from app.ocr import run_ocr_multi  # type: ignore

    results: List[ZoneEngineResult] = []

    for engine_name in zone.engines:
        t0 = time.monotonic()
        try:
            ocr_map = run_ocr_multi(image_bytes, [engine_name])
            elapsed = (time.monotonic() - t0) * 1000.0

            if engine_name in ocr_map:
                r = ocr_map[engine_name]
                # run_ocr_multi returns OCRResult with text="" and confidence=0.0 on failure
                # We treat confidence=0.0 with text="" as a potential engine failure,
                # but we still return it as a valid result (the OCR ran successfully).
                results.append(
                    ZoneEngineResult(
                        engine=engine_name,
                        text=r.text,
                        confidence=r.confidence if r.confidence != 0.0 or r.text else None,
                        latency_ms=elapsed,
                        error=None,
                    )
                )
            else:
                elapsed = (time.monotonic() - t0) * 1000.0
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
            logger.warning(
                "dispatch_zone_ocr: engine=%s exception=%s", engine_name, exc
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
