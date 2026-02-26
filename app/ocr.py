"""
OCR module: Google Vision + Azure Computer Vision + OCR.Space.
Supports running a specific engine or a set of engines.

Batching extension (Phase google-batch-v2):
  google_batch_annotate_images(image_bytes_list) — batch helper.
  _GOOGLE_CACHE — thread-local result cache populated by run_routes before
  dispatch; consumed once by _ocr_google so dispatcher path is unchanged.
"""
import math
import os
import logging
import httpx
import base64
import threading
from typing import Optional

logger = logging.getLogger(__name__)

ALL_ENGINES = ["google", "azure", "ocrspace"]

_GOOGLE_CACHE_LOCK = threading.Lock()
# Maps id(image_bytes) -> OCRResult for pre-computed Google results.
# Entries are consumed once (removed on first read) to avoid stale state.
_GOOGLE_CACHE: dict[int, "OCRResult"] = {}


def _google_cache_put(image_bytes: bytes, result: "OCRResult") -> None:
    """Store a pre-computed Google result keyed by object identity."""
    with _GOOGLE_CACHE_LOCK:
        _GOOGLE_CACHE[id(image_bytes)] = result


def _google_cache_pop(image_bytes: bytes) -> "Optional[OCRResult]":
    """Consume a cached Google result (remove on read). Returns None if absent."""
    with _GOOGLE_CACHE_LOCK:
        return _GOOGLE_CACHE.pop(id(image_bytes), None)


def _google_cache_clear(keys: list[int]) -> None:
    """Remove any remaining cache entries for the given ids (cleanup)."""
    with _GOOGLE_CACHE_LOCK:
        for k in keys:
            _GOOGLE_CACHE.pop(k, None)


# ─────────────────────────── Startup validation ──────────────────────────────

_AZURE_WARN_EMITTED = False


def _check_azure_config() -> None:
    """
    Called once at startup (via _ensure_azure_checked).
    Logs a structured WARNING if azure is listed in ALL_ENGINES but
    env vars are missing.  Never raises.
    """
    global _AZURE_WARN_EMITTED
    if _AZURE_WARN_EMITTED:
        return
    _AZURE_WARN_EMITTED = True

    endpoint = os.getenv("AZURE_OCR_ENDPOINT", "").strip()
    key = os.getenv("AZURE_OCR_KEY", "").strip()

    if "azure" in ALL_ENGINES:
        if not endpoint and not key:
            logger.warning(
                '{"event": "azure_config_warning", "message": '
                '"azure is in ALL_ENGINES but AZURE_OCR_ENDPOINT and AZURE_OCR_KEY are not set; '
                'azure will be skipped for every OCR call"}'
            )
        elif not endpoint:
            logger.warning(
                '{"event": "azure_config_warning", "message": '
                '"azure is in ALL_ENGINES but AZURE_OCR_ENDPOINT is not set; '
                'azure will be skipped for every OCR call"}'
            )
        elif not key:
            logger.warning(
                '{"event": "azure_config_warning", "message": '
                '"azure is in ALL_ENGINES but AZURE_OCR_KEY is not set; '
                'azure will be skipped for every OCR call"}'
            )
        else:
            logger.info(
                '{"event": "azure_config_ok", "message": '
                '"azure env vars present"}'
            )


def emit_startup_warnings() -> None:
    """Call once from app lifespan to emit engine config warnings."""
    _check_azure_config()


# ─────────────────────────── Google Vision ───────────────────────────────────

def _parse_google_full_text(full_text_annotation) -> tuple[str, float]:
    """
    Shared response-parsing logic for both single and batch Google calls.
    Returns (text, avg_confidence).
    """
    confidences = []
    for page in full_text_annotation.pages:
        for block in page.blocks:
            if block.confidence:
                confidences.append(block.confidence)
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.5
    return (full_text_annotation.text.strip(), avg_conf)


def _build_google_annotate_request(image_bytes: bytes):
    """Build a single AnnotateImageRequest for DOCUMENT_TEXT_DETECTION."""
    from google.cloud import vision  # type: ignore
    return vision.AnnotateImageRequest(
        image=vision.Image(content=image_bytes),
        features=[vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)],
    )


def _ocr_google(image_bytes: bytes) -> Optional[tuple[str, float]]:
    """Return (text, confidence) or None on failure.

    If a pre-computed result has been injected via _google_cache_put, consume
    it and return immediately — no API call made.
    """
    # Check pre-computed cache first (set by run_routes batch path).
    cached = _google_cache_pop(image_bytes)
    if cached is not None:
        if not cached.text and cached.confidence == 0.0:
            return None
        return (cached.text, cached.confidence)

    try:
        from google.cloud import vision  # type: ignore
        client = vision.ImageAnnotatorClient()
        image = vision.Image(content=image_bytes)
        response = client.document_text_detection(image=image)
        if response.error.message:
            logger.warning("Google Vision error: %s", response.error.message)
            return None
        full = response.full_text_annotation
        if not full or not full.text:
            return None
        return _parse_google_full_text(full)
    except Exception as exc:
        logger.warning("Google Vision exception: %s", exc)
        return None


def google_batch_annotate_images(image_bytes_list: list[bytes]) -> list["OCRResult"]:
    """
    Batch Google Vision DOCUMENT_TEXT_DETECTION for up to 16 images per call.

    - Chunks input into groups of 16.
    - Preserves input order strictly.
    - Never raises; failed elements return OCRResult(text='', confidence=0.0,
      engine='google').
    - Logs "google_batch_v2 size=N chunks=C" when size >= 2.
    """
    n = len(image_bytes_list)
    if n == 0:
        return []

    chunk_size = 16
    num_chunks = math.ceil(n / chunk_size)

    if n >= 2:
        logger.info("google_batch_v2 size=%d chunks=%d", n, num_chunks)

    results: list[OCRResult] = []

    try:
        from google.cloud import vision  # type: ignore
        client = vision.ImageAnnotatorClient()
    except Exception as exc:
        logger.warning("google_batch_v2 client_init_failed: %s", exc)
        return [OCRResult("", 0.0, "google") for _ in image_bytes_list]

    for chunk_start in range(0, n, chunk_size):
        chunk = image_bytes_list[chunk_start: chunk_start + chunk_size]
        requests = [_build_google_annotate_request(img) for img in chunk]
        try:
            batch_response = client.batch_annotate_images(requests=requests)
            responses = batch_response.responses
            # Pad if API returns fewer responses than requested
            while len(responses) < len(chunk):
                responses.append(None)
            for i, resp in enumerate(responses):
                idx = chunk_start + i
                if resp is None:
                    logger.warning(
                        "google_batch_v2 element_failed index=%d error=missing_response", idx
                    )
                    results.append(OCRResult("", 0.0, "google"))
                    continue
                if resp.error.message:
                    logger.warning(
                        "google_batch_v2 element_failed index=%d error=%s",
                        idx, resp.error.message,
                    )
                    results.append(OCRResult("", 0.0, "google"))
                    continue
                full = resp.full_text_annotation
                if not full or not full.text:
                    results.append(OCRResult("", 0.0, "google"))
                    continue
                text, conf = _parse_google_full_text(full)
                results.append(OCRResult(text, conf, "google"))
        except Exception as exc:
            logger.warning(
                "google_batch_v2 chunk_failed chunk_start=%d error=%s",
                chunk_start, exc,
            )
            results.extend([OCRResult("", 0.0, "google")] * len(chunk))

    return results


# ─────────────────────────── Azure OCR ───────────────────────────────────────

def _ocr_azure(image_bytes: bytes) -> Optional[tuple[str, float]]:
    """Return (text, confidence) or None on failure."""
    endpoint = os.getenv("AZURE_OCR_ENDPOINT", "").rstrip("/")
    key = os.getenv("AZURE_OCR_KEY", "")
    if not endpoint or not key:
        return None
    url = f"{endpoint}/computervision/imageanalysis:analyze"
    params = {"features": "read", "api-version": "2023-02-01-preview"}
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/octet-stream"}
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, params=params, headers=headers, content=image_bytes)
            r.raise_for_status()
            data = r.json()
        read_result = data.get("readResult", {})
        blocks = read_result.get("blocks", [])
        lines_text, confidences = [], []
        for block in blocks:
            for line in block.get("lines", []):
                lines_text.append(line.get("text", ""))
                for word in line.get("words", []):
                    conf = word.get("confidence")
                    if conf is not None:
                        confidences.append(conf)
        if not lines_text:
            return None
        text = "\n".join(lines_text).strip()
        avg_conf = sum(confidences) / len(confidences) if confidences else 0.5
        return (text, avg_conf)
    except Exception as exc:
        logger.warning("Azure OCR exception: %s", exc)
        return None


# ─────────────────────────── OCR.Space ───────────────────────────────────────

def _ocr_ocrspace(image_bytes: bytes) -> Optional[tuple[str, float]]:
    """
    OCR.Space Engine 3 — confidence is a fixed approximation
    (OCR.Space API does not return word-level confidence).
    exit_code 1 = success → 0.75, exit_code 2 = partial → 0.50.
    """
    api_key = os.getenv("OCR_SPACE_API_KEY", "").strip()
    if not api_key:
        return None
    if image_bytes[:4] == b'\x89PNG':
        mime = "image/png"
    elif image_bytes[:2] == b'\xff\xd8':
        mime = "image/jpeg"
    elif image_bytes[:4] == b'GIF8':
        mime = "image/gif"
    else:
        mime = "image/jpeg"
    img_b64 = base64.b64encode(image_bytes).decode()
    try:
        with httpx.Client(timeout=60) as client:
            r = client.post(
                "https://api.ocr.space/parse/image",
                data={
                    "apikey": api_key,
                    "base64Image": f"data:{mime};base64,{img_b64}",
                    "language": "auto",
                    "OCREngine": "3",
                    "scale": "true",
                    "detectOrientation": "true",
                    "isOverlayRequired": "false",
                },
            )
            r.raise_for_status()
            data = r.json()
        if data.get("IsErroredOnProcessing"):
            err_msgs = data.get("ErrorMessage", [])
            logger.warning("OCR.Space error: %s", err_msgs)
            return None
        exit_code = data.get("OCRExitCode", 0)
        if exit_code not in (1, 2):
            return None
        parsed = data.get("ParsedResults", [])
        if not parsed:
            return None
        text = parsed[0].get("ParsedText", "").strip()
        if not text:
            return None
        confidence = 0.75 if exit_code == 1 else 0.50
        return (text, confidence)
    except Exception as exc:
        logger.warning("OCR.Space exception: %s", exc)
        return None


_ENGINE_FNS = {
    "google":   _ocr_google,
    "azure":    _ocr_azure,
    "ocrspace": _ocr_ocrspace,
}


class OCRResult:
    def __init__(self, text: str, confidence: float, engine: str):
        self.text = text
        self.confidence = confidence
        self.engine = engine

    def to_dict(self) -> dict:
        return {"text": self.text, "confidence": self.confidence, "engine": self.engine}


def run_ocr(image_bytes: bytes, engine: str = None) -> OCRResult:
    """Run a single engine (or best-of-all if engine is None)."""
    results: list[tuple[str, float, str]] = []
    engines = [engine] if engine and engine in _ENGINE_FNS else list(_ENGINE_FNS.keys())
    for eng_name in engines:
        r = _ENGINE_FNS[eng_name](image_bytes)
        if r:
            results.append((r[0], r[1], eng_name))
    if not results:
        return OCRResult("", 0.0, engine or "none")
    results.sort(key=lambda x: x[1], reverse=True)
    text, conf, eng = results[0]
    return OCRResult(text, conf, eng)


def run_ocr_multi(image_bytes: bytes, engines: list[str]) -> dict[str, OCRResult]:
    """
    Run each engine in `engines`, return dict {engine_name: OCRResult}.
    Missing / failed engines are not included in the result dict.
    """
    out: dict[str, OCRResult] = {}
    for eng_name in engines:
        fn = _ENGINE_FNS.get(eng_name)
        if fn is None:
            continue
        r = fn(image_bytes)
        if r:
            out[eng_name] = OCRResult(r[0], r[1], eng_name)
        else:
            out[eng_name] = OCRResult("", 0.0, eng_name)
    return out
