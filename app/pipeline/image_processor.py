"""
Image preprocessing helpers for Phase 3 (v2 Canonical).
Implements: maybe_upscale, scale_bbox, crop_zone_to_png.
"""
from __future__ import annotations

import base64
import io
import math
from typing import List, Tuple

try:
    from PIL import Image
    _PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PILLOW_AVAILABLE = False

# Minimum dimensions that trigger upscaling
_MIN_W = 800
_MIN_H = 800


def pillow_available() -> bool:
    return _PILLOW_AVAILABLE


def load_image(image_bytes: bytes) -> "Image.Image":
    """Load image from bytes. Raises ValueError on failure."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.load()  # force decode
        return img
    except Exception as exc:
        raise ValueError(f"Cannot decode image: {exc}") from exc


def maybe_upscale(
    img: "Image.Image",
    min_w: int = _MIN_W,
    min_h: int = _MIN_H,
) -> Tuple["Image.Image", bool]:
    """
    Upscale image if either dimension is below threshold.
    Returns (processed_img, upscaled_flag).
    Preserves aspect ratio. Uses LANCZOS resampler.
    """
    w, h = img.size
    if w < min_w or h < min_h:
        scale = max(min_w / w, min_h / h)
        new_w = round(w * scale)
        new_h = round(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        return img, True
    return img, False


def scale_bbox(
    bbox_source: List[int],
    source_size: List[int],
    actual_size: List[int],
) -> List[int]:
    """
    Scale bbox from source_size pixel space to actual_size pixel space.
    Uses floor for x1/y1, ceil for x2/y2 then clamps.
    Returns [x1, y1, x2, y2].
    """
    x1, y1, x2, y2 = bbox_source
    source_w, source_h = source_size
    proc_w, proc_h = actual_size

    scale_x = proc_w / source_w
    scale_y = proc_h / source_h

    x1_s = math.floor(x1 * scale_x)
    y1_s = math.floor(y1 * scale_y)
    x2_s = math.ceil(x2 * scale_x)
    y2_s = math.ceil(y2 * scale_y)

    # Clamp
    x1_s = max(0, min(x1_s, proc_w - 1))
    y1_s = max(0, min(y1_s, proc_h - 1))
    x2_s = max(1, min(x2_s, proc_w))
    y2_s = max(1, min(y2_s, proc_h))

    # Ensure non-degenerate
    if x2_s <= x1_s:
        x2_s = min(proc_w, x1_s + 1)
    if y2_s <= y1_s:
        y2_s = min(proc_h, y1_s + 1)

    return [x1_s, y1_s, x2_s, y2_s]


def crop_zone_to_png(img: "Image.Image", bbox_scaled: List[int]) -> bytes:
    """
    Crop img to bbox_scaled and return PNG bytes.
    Does NOT write to disk.
    """
    x1, y1, x2, y2 = bbox_scaled
    cropped = img.crop((x1, y1, x2, y2))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue()


def crop_to_base64(img: "Image.Image", bbox_scaled: List[int]) -> str:
    """Return base64-encoded PNG (no prefix) for a zone crop."""
    png_bytes = crop_zone_to_png(img, bbox_scaled)
    return base64.b64encode(png_bytes).decode("ascii")
