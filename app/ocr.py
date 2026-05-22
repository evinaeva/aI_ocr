"""
OCR module: Google Vision + Azure Computer Vision + OCR.Space.
Supports running a specific engine or a set of engines.

Batching extension (Phase google-batch-v2):
  google_batch_annotate_images(image_bytes_list) — batch helper.
  _GOOGLE_CACHE — thread-safe result cache populated by run_routes before
  dispatch; consumed once by _ocr_google so dispatcher path is unchanged.
"""
import math
import os
import logging
import httpx
import base64
import threading
from typing import Optional

from app.metrics.engine_usage import increment_engine_usage

logger = logging.getLogger(__name__)

ALL_ENGINES = ["google", "azure", "ocrspace"]
DEFAULT_AZURE_OCR_API_VERSION = "2024-02-01"

_GOOGLE_CACHE_LOCK = threading.Lock()
# Maps id(image_bytes) -> OCRResult for pre-computed Google results.
# Entries are consumed once (removed on first read) to avoid stale state.
_GOOGLE_CACHE: dict = {}


def _google_cache_put(image_bytes: bytes, result: "OCRResult") -> None:
    """Store a pre-computed Google result keyed by object identity."""
    with _GOOGLE_CACHE_LOCK:
        _GOOGLE_CACHE[id(image_bytes)] = result


def _google_cache_pop(image_bytes: bytes) -> "Optional[OCRResult]":
    """Consume a cached Google result (remove on read). Returns None if absent."""
    with _GOOGLE_CACHE_LOCK:
        return _GOOGLE_CACHE.pop(id(image_bytes), None)


def _google_cache_clear(keys: list) -> None:
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

def _parse_google_full_text(full_text_annotation):
    """
    Shared response-parsing logic for both single and batch Google calls.
    Returns (text, avg_confidence).
    """
    confidences = []
    for page in full_text_annotation.pages:
        for block in page.blocks:
            if block.confidence:
                confidences.append(block.confidence)
    avg_conf = sum(confidences) / len(confidences) if confidences else None
    return (full_text_annotation.text.strip(), avg_conf)


def _parse_google_text_annotations(response) -> Optional[tuple]:
    """
    Parse TEXT_DETECTION response.
    Extracts text from text_annotations[0].description.
    Extracts confidence from full_text_annotation.pages[].blocks[] when
    present — TEXT_DETECTION responses include full_text_annotation alongside
    text_annotations, so block-level confidence is available without any
    extra request parameter.
    Returns (text, avg_confidence) or None if no text found.
    """
    anns = getattr(response, "text_annotations", None)
    if not anns:
        return None
    first = anns[0] if len(anns) > 0 else None
    text = ((getattr(first, "description", "") or "").strip() if first else "")
    if not text:
        return None

    # Attempt to extract confidence from full_text_annotation blocks.
    # full_text_annotation is populated by Google Vision for TEXT_DETECTION
    # responses (same as DOCUMENT_TEXT_DETECTION), providing block.confidence
    # values in range [0, 1]. We compute the arithmetic mean across all blocks.
    avg_conf = None
    full = getattr(response, "full_text_annotation", None)
    if full and getattr(full, "pages", None):
        confidences = []
        for page in full.pages:
            for block in page.blocks:
                if block.confidence:
                    confidences.append(block.confidence)
        avg_conf = sum(confidences) / len(confidences) if confidences else None

    return (text, avg_conf)

def _google_feature_for_mode(vision, google_mode: Optional[str]):
    mode = (google_mode or "text").strip().lower()
    if mode in ("document", "document_text_detection"):
        return vision.Feature.Type.DOCUMENT_TEXT_DETECTION
    return vision.Feature.Type.TEXT_DETECTION


def _build_google_annotate_request(image_bytes: bytes, google_mode: Optional[str] = None):
    """Build a single AnnotateImageRequest for Google Vision mode."""
    from google.cloud import vision  # type: ignore
    return vision.AnnotateImageRequest(
        image=vision.Image(content=image_bytes),
        features=[vision.Feature(type_=_google_feature_for_mode(vision, google_mode))],
    )


def _ocr_google(image_bytes: bytes, google_mode: Optional[str] = None) -> Optional[tuple]:
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
        mode = (google_mode or "text").strip().lower()
        if mode in ("document", "document_text_detection"):
            response = client.document_text_detection(image=image)
        else:
            response = client.text_detection(image=image)
        increment_engine_usage("google")
        if response.error.message:
            logger.warning("Google Vision error: %s", response.error.message)
            return None
        if mode in ("document", "document_text_detection"):
            full = response.full_text_annotation
            if not full or not full.text:
                return None
            return _parse_google_full_text(full)
        return _parse_google_text_annotations(response)
    except Exception as exc:
        logger.warning("Google Vision exception: %s", exc)
        return None


def google_batch_annotate_images(image_bytes_list: list, google_mode: Optional[str] = None) -> list:
    """
    Batch Google Vision (TEXT_DETECTION or DOCUMENT_TEXT_DETECTION) for up to 16 images per call.

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

    results = []

    try:
        from google.cloud import vision  # type: ignore
        client = vision.ImageAnnotatorClient()
    except Exception as exc:
        logger.warning("google_batch_v2 client_init_failed: %s", exc)
        return [OCRResult("", 0.0, "google") for _ in image_bytes_list]

    for chunk_start in range(0, n, chunk_size):
        chunk = image_bytes_list[chunk_start: chunk_start + chunk_size]
        try:
            from google.cloud import vision  # type: ignore
        except Exception:
            results.extend([OCRResult("", 0.0, "google")] * len(chunk))
            continue
        requests = [
            vision.AnnotateImageRequest(
                image=vision.Image(content=img),
                features=[vision.Feature(type_=_google_feature_for_mode(vision, google_mode))],
            )
            for img in chunk
        ]
        try:
            batch_response = client.batch_annotate_images(requests=requests)
            increment_engine_usage("google")
            responses = list(batch_response.responses)
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
                mode = (google_mode or "text").strip().lower()
                if mode in ("document", "document_text_detection"):
                    full = resp.full_text_annotation
                    if not full or not full.text:
                        results.append(OCRResult("", 0.0, "google"))
                        continue
                    text, conf = _parse_google_full_text(full)
                    results.append(OCRResult(text, conf, "google"))
                else:
                    parsed = _parse_google_text_annotations(resp)
                    if not parsed:
                        results.append(OCRResult("", 0.0, "google"))
                        continue
                    text, conf = parsed
                    results.append(OCRResult(text, conf, "google"))
        except Exception as exc:
            logger.warning(
                "google_batch_v2 chunk_failed chunk_start=%d error=%s",
                chunk_start, exc,
            )
            results.extend([OCRResult("", 0.0, "google")] * len(chunk))

    return results


# ─────────────────────────── Azure OCR ───────────────────────────────────────

AZURE_MAX_ATTEMPTS = 2  # initial + 1 retry (per operator feedback 2026-05)

# Markdown-style heading prefixes (`#`, `##`, `###`, `####`) at line start
# show up in both Azure (Image Analysis 4.0 layout hints on visually-large
# banner text) AND OCR.Space (post-PR-#82 the operator confirmed via
# screenshot that the offender column was actually OCR.Space, not Azure).
# Strip per engine adapter — they're never real content.
import re as _re_md_hdr
_MARKDOWN_HEADER_RE = _re_md_hdr.compile(r"^[#]+\s*", _re_md_hdr.MULTILINE)


def _strip_markdown_headers(text: str) -> str:
    """Remove leading `#`/`##`/... markdown headers per line."""
    return _MARKDOWN_HEADER_RE.sub("", text)


# Backwards-compatible alias — older tests reference `_strip_azure_markdown`.
_strip_azure_markdown = _strip_markdown_headers


def _ocr_azure_once(image_bytes: bytes, endpoint: str, key: str, attempt: int) -> Optional[tuple]:
    """Single Azure attempt. Returns (text, avg_conf) or None on failure or empty response."""
    url = f"{endpoint}/computervision/imageanalysis:analyze"
    # Azure OCR target: Image Analysis 4.0 Read.
    # Example stable api-version is 2024-02; exact value is environment-configurable
    # for safe rollout/testing and defaults to legacy preview for backwards compatibility.
    api_version = os.getenv("AZURE_OCR_API_VERSION", DEFAULT_AZURE_OCR_API_VERSION).strip()
    if not api_version:
        api_version = DEFAULT_AZURE_OCR_API_VERSION
    params = {"features": "read", "api-version": api_version}
    headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/octet-stream"}
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, params=params, headers=headers, content=image_bytes)
            increment_engine_usage("azure")
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
            logger.info("Azure OCR empty response (attempt %d)", attempt)
            return None
        text = "\n".join(lines_text).strip()
        text = _strip_markdown_headers(text)
        avg_conf = sum(confidences) / len(confidences) if confidences else None
        # Visibility for operator-reported "Azure sometimes returns one
        # phrase, sometimes everything". Flag suspiciously partial answers
        # (very few words / very low confidence) so they're searchable in
        # Cloud Run logs. We still return the text — consensus + LLM judge
        # downstream decide what to do with it.
        word_count = len(text.split())
        if word_count <= 3 or (avg_conf is not None and avg_conf < 0.4):
            logger.warning(
                "azure_partial_response attempt=%d words=%d avg_conf=%s text=%r",
                attempt, word_count, f"{avg_conf:.3f}" if avg_conf else "n/a",
                text[:80],
            )
        return (text, avg_conf)
    except Exception as exc:
        logger.warning("Azure OCR exception (attempt %d): %s", attempt, exc)
        return None


def _ocr_azure(image_bytes: bytes) -> Optional[tuple]:
    """Return (text, confidence) or None on failure, with one retry on empty/exception.

    Operators observed Azure intermittently returning no response for small crops;
    a single retry recovers most of those cases at negligible cost.
    """
    endpoint = os.getenv("AZURE_OCR_ENDPOINT", "").rstrip("/")
    key = os.getenv("AZURE_OCR_KEY", "")
    if not endpoint or not key:
        return None
    for attempt in range(1, AZURE_MAX_ATTEMPTS + 1):
        result = _ocr_azure_once(image_bytes, endpoint, key, attempt)
        if result is not None:
            return result
    logger.warning("Azure OCR gave up after %d attempts", AZURE_MAX_ATTEMPTS)
    return None


# ─────────────────────────── OCR.Space ───────────────────────────────────────

OCRSPACE_MAX_ATTEMPTS = 3


def _ocr_ocrspace_once(api_key, mime, img_b64, attempt):
    """One OCR.Space request. Returns (text, None) on success, None on transient/API failure."""
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
            increment_engine_usage("ocrspace")
            r.raise_for_status()
            data = r.json()
        if data.get("IsErroredOnProcessing"):
            err_msgs = data.get("ErrorMessage", [])
            logger.warning("OCR.Space error (attempt %d): %s", attempt, err_msgs)
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
        # Strip OCR.Space's markdown-style heading prefixes (`#`, `##`, ...)
        # at line starts — newer engine versions emit them on banner-sized
        # text. The hashes are layout hints, never real content, and cause
        # consensus disagreement with Google's plain output.
        text = _strip_markdown_headers(text)
        # OCR.Space sometimes returns punctuation-only garbage on tight crops
        # (e.g. long runs of `#` or `.`) that wins consensus by length and
        # poisons downstream comparison. Treat any response without a single
        # letter or digit as a non-response.
        if not any(ch.isalnum() for ch in text):
            logger.info("OCR.Space punctuation-only response (attempt %d), discarding", attempt)
            return None
        return (text, None)
    except Exception as exc:
        logger.warning("OCR.Space exception (attempt %d): %s", attempt, exc)
        return None


def _ocr_ocrspace(image_bytes: bytes) -> Optional[tuple]:
    """OCR.Space Engine 3 with up to OCRSPACE_MAX_ATTEMPTS retries on
    transient or empty responses. Confidence is returned as None — the
    API does not provide stable per-word confidence in this flow.

    Retries are sequential: OCR.Space rate-limits per API key, so
    parallel retries against the same key would only trip the same
    server-side limit. Each attempt re-uses the same payload.
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
    for attempt in range(1, OCRSPACE_MAX_ATTEMPTS + 1):
        result = _ocr_ocrspace_once(api_key, mime, img_b64, attempt)
        if result is not None:
            return result
    logger.warning("OCR.Space gave up after %d attempts", OCRSPACE_MAX_ATTEMPTS)
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
    results = []
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


def run_ocr_multi(image_bytes: bytes, engines: list, engine_config: Optional[dict] = None) -> dict:
    """
    Run each engine in `engines`, return dict {engine_name: OCRResult}.
    Missing / failed engines are not included in the result dict.
    """
    out = {}
    for eng_name in engines:
        fn = _ENGINE_FNS.get(eng_name)
        if fn is None:
            continue
        if eng_name == "google":
            google_mode = (engine_config or {}).get("google_mode")
            r = fn(image_bytes, google_mode)
        else:
            r = fn(image_bytes)
        if r:
            out[eng_name] = OCRResult(r[0], r[1], eng_name)
        else:
            out[eng_name] = OCRResult("", 0.0, eng_name)
    return out
