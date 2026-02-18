"""
OCR module: Google Vision + Azure Computer Vision + OCR.Space.
Returns the result with the highest confidence.
"""
import os
import logging
import httpx
import base64
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────── Google Vision ───────────────────────────────────

def _ocr_google(image_bytes: bytes) -> Optional[tuple[str, float]]:
    """Return (text, confidence) or None on failure."""
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

        confidences = []
        for page in full.pages:
            for block in page.blocks:
                if block.confidence:
                    confidences.append(block.confidence)

        avg_conf = sum(confidences) / len(confidences) if confidences else 0.5
        return (full.text.strip(), avg_conf)

    except Exception as exc:
        logger.warning("Google Vision exception: %s", exc)
        return None


# ─────────────────────────── Azure OCR ───────────────────────────────────────

def _ocr_azure(image_bytes: bytes) -> Optional[tuple[str, float]]:
    """Return (text, confidence) or None on failure."""
    endpoint = os.getenv("AZURE_OCR_ENDPOINT", "").rstrip("/")
    key = os.getenv("AZURE_OCR_KEY", "")
    if not endpoint or not key:
        return None

    url = f"{endpoint}/computervision/imageanalysis:analyze"
    params = {
        "features": "read",
        "api-version": "2023-02-01-preview",
    }
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/octet-stream",
    }
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(url, params=params, headers=headers, content=image_bytes)
            r.raise_for_status()
            data = r.json()

        read_result = data.get("readResult", {})
        blocks = read_result.get("blocks", [])
        lines_text = []
        confidences = []

        for block in blocks:
            for line in block.get("lines", []):
                lines_text.append(line.get("text", ""))
                for word in line.get("words", []):
                    conf = word.get("confidence", None)
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
    OCR.Space Engine 3: 200+ languages, auto-detect.
    No language mapping needed — uses language=auto.
    API key from env OCR_SPACE_API_KEY (falls back to 'helloworld').
    """
    api_key = os.getenv("OCR_SPACE_API_KEY", "").strip()
    if not api_key:
        logger.debug("OCR.Space: no API key, skipping")
        return None

    img_b64 = base64.b64encode(image_bytes).decode()

    # Detect mime type from magic bytes
    if image_bytes[:4] == b'\x89PNG':
        mime = "image/png"
    elif image_bytes[:2] == b'\xff\xd8':
        mime = "image/jpeg"
    elif image_bytes[:4] == b'GIF8':
        mime = "image/gif"
    else:
        mime = "image/jpeg"  # fallback

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
            err_str = "; ".join(err_msgs) if isinstance(err_msgs, list) else str(err_msgs)
            logger.warning("OCR.Space error: %s", err_str)
            return None

        exit_code = data.get("OCRExitCode", 0)
        if exit_code not in (1, 2):  # 1=success, 2=partial success
            logger.warning("OCR.Space exit code: %d", exit_code)
            return None

        parsed = data.get("ParsedResults", [])
        if not parsed:
            return None

        text = parsed[0].get("ParsedText", "").strip()
        if not text:
            return None

        # OCR.Space doesn't return per-word confidence for engine 3;
        # use exit code as rough proxy
        confidence = 0.75 if exit_code == 1 else 0.5

        return (text, confidence)

    except Exception as exc:
        logger.warning("OCR.Space exception: %s", exc)
        return None


# ─────────────────────────── Public API ──────────────────────────────────────

class OCRResult:
    def __init__(self, text: str, confidence: float, engine: str):
        self.text = text
        self.confidence = confidence
        self.engine = engine

    def to_dict(self) -> dict:
        return {"text": self.text, "confidence": self.confidence, "engine": self.engine}


def run_ocr(image_bytes: bytes) -> OCRResult:
    """
    Run Google Vision, Azure, and OCR.Space.
    Return result with highest confidence.
    Falls back to whichever one succeeds.
    """
    results: list[tuple[str, float, str]] = []

    g = _ocr_google(image_bytes)
    if g:
        results.append((g[0], g[1], "google"))

    a = _ocr_azure(image_bytes)
    if a:
        results.append((a[0], a[1], "azure"))

    s = _ocr_ocrspace(image_bytes)
    if s:
        results.append((s[0], s[1], "ocrspace"))

    if not results:
        return OCRResult("", 0.0, "none")

    # Pick highest confidence
    results.sort(key=lambda x: x[1], reverse=True)
    text, conf, engine = results[0]
    logger.info("OCR selected engine=%s conf=%.3f len=%d (candidates=%d)",
                engine, conf, len(text), len(results))
    return OCRResult(text, conf, engine)
