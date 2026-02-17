"""
OCR module: Google Vision + Azure Computer Vision.
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

        # Collect page-level confidence
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
    Run both Google Vision and Azure, return result with higher confidence.
    Falls back to whichever one succeeds.
    """
    results: list[tuple[str, float, str]] = []

    g = _ocr_google(image_bytes)
    if g:
        results.append((g[0], g[1], "google"))

    a = _ocr_azure(image_bytes)
    if a:
        results.append((a[0], a[1], "azure"))

    if not results:
        return OCRResult("", 0.0, "none")

    # Pick highest confidence
    results.sort(key=lambda x: x[1], reverse=True)
    text, conf, engine = results[0]
    logger.info("OCR selected engine=%s conf=%.3f len=%d", engine, conf, len(text))
    return OCRResult(text, conf, engine)
