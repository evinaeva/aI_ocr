"""
OCR module: Google Vision + Azure Computer Vision + OCR.Space.
Supports running a specific engine or a set of engines.
"""
import os
import logging
import httpx
import base64
from typing import Optional

logger = logging.getLogger(__name__)

ALL_ENGINES = ["google", "azure", "ocrspace"]


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
