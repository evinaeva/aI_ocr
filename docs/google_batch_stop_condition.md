# Google Vision Batching — STOP CONDITION

## Date: 2026-02-26

## Finding

The current architecture calls `_ocr_google(image_bytes: bytes)` once per **single image**.
No code path ever aggregates multiple images into one Google Vision API call.

**STOP CONDITION applies.**

---

## Exact location of single-image calls

| File | Function | Line(s) | Notes |
|------|----------|---------|-------|
| `app/ocr.py` | `run_ocr` | iterates `_ENGINE_FNS[eng_name](image_bytes)` | called with one image per invocation |
| `app/ocr.py` | `run_ocr_multi` | iterates `fn(image_bytes)` per engine | one image per call, multiple engines |
| `app/ocr.py` | `_ocr_google` | `client.document_text_detection(image=image)` | **single-image SDK call** |

The callers (`run_routes.py`, `zip_processor.py`) invoke `run_ocr` / `run_ocr_multi` once per zone image.
There is **no aggregation point** where N images are passed together to the Google path.

---

## Proposed minimal integration point

The only sensible integration point is in `app/pipeline/run_routes.py` (or the dispatcher),
where zone images for a single run could be collected and passed as a list to `_ocr_google_batch`
before dispatching individual results back.

**Proposed function to add (in `app/ocr.py` only):**

```
Function: _ocr_google_batch(images: list[bytes]) -> list[OCRResult]
Scope:    app/ocr.py, Google-specific branch only
```

**Minimal pseudo-diff (≤15 lines):**

```python
# app/ocr.py — ADD after _ocr_google(), MODIFY nothing else

import math

CHUNK_SIZE = 16

def _ocr_google_batch(images: list[bytes]) -> list[OCRResult]:
    """Batch Google Vision calls. Chunk size 16. Order preserved."""
    from google.cloud import vision
    client = vision.ImageAnnotatorClient()
    n = len(images)
    num_chunks = math.ceil(n / CHUNK_SIZE)
    if n >= 2:
        logger.info("google_batch size=%d chunks=%d", n, num_chunks)
    results: list[OCRResult] = []
    for chunk_start in range(0, n, CHUNK_SIZE):
        chunk = images[chunk_start: chunk_start + CHUNK_SIZE]
        requests = [vision.AnnotateImageRequest(
            image=vision.Image(content=img),
            features=[vision.Feature(type_=vision.Feature.Type.DOCUMENT_TEXT_DETECTION)]
        ) for img in chunk]
        try:
            batch_response = client.batch_annotate_images(requests=requests)
            for i, resp in enumerate(batch_response.responses):
                idx = chunk_start + i
                if resp.error.message:
                    logger.warning("google_batch element_failed index=%d error=%s", idx, resp.error.message)
                    results.append(OCRResult("", 0.0, "google"))
                    continue
                full = resp.full_text_annotation
                if not full or not full.text:
                    results.append(OCRResult("", 0.0, "google"))
                    continue
                confs = [b.confidence for p in full.pages for b in p.blocks if b.confidence]
                avg = sum(confs) / len(confs) if confs else 0.5
                results.append(OCRResult(full.text.strip(), avg, "google"))
            # pad if response list shorter than chunk
            while len(results) < chunk_start + len(chunk):
                results.append(OCRResult("", 0.0, "google"))
        except Exception as exc:
            logger.warning("google_batch chunk failed: %s", exc)
            results.extend([OCRResult("", 0.0, "google")] * len(chunk))
    return results
```

---

## Status

**WAITING FOR APPROVAL** before implementing or wiring this helper.

Caller integration would require modifying `run_routes.py` / dispatcher,
which is **outside the allowed scope** (`app/ocr.py` only per contract §IMPLEMENTATION RULES §4).

If batching is only to be used when the caller already aggregates images,
and the current caller never does so, **batching cannot be exercised end-to-end without a caller change.**

Please confirm:
1. Whether to proceed with adding `_ocr_google_batch` to `app/ocr.py` + unit tests (no caller wiring), OR
2. Whether the caller integration scope should be expanded.
