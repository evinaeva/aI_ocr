"""Detect text blocks on a banner image.

Wraps easyocr's CRAFT-based detector (it bundles CRAFT under the hood
and ships with modern dependencies — the standalone `craft-text-detector`
PyPI package pins numpy==1.21.2, which doesn't install on Python 3.14).

We only use the **detection** half of easyocr (set `recognizer=False`)
because banner-QA compares reference text *visually* via skeletons; we
never want easyocr's OCR output near our verdict.

Detection thresholds are tuned for marketing banners with heavy
decorative effects (glow, outlines, shadow): `text_threshold=0.55`
and `low_text=0.3` — lower than easyocr defaults (0.7 / 0.4) to keep
stylised characters from being lost. Source: repo-analysis 2026-05-26.

The CRAFT model weights (~80MB) are downloaded on first call.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image


@dataclass
class BBox:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    def crop(self, img: Image.Image) -> Image.Image:
        return img.crop((self.x, self.y, self.x2, self.y2))


# Tuned for banner-style imagery; lower than easyocr defaults to keep
# stylised glyphs from being missed. See module docstring.
DEFAULT_TEXT_THRESHOLD = 0.55
DEFAULT_LOW_TEXT = 0.30
DEFAULT_LINK_THRESHOLD = 0.40


_READER = None


def _get_reader():
    """Lazy-initialise a detection-only easyocr.Reader.

    `verbose=False` silences a Unicode progress bar that crashes on Windows
    cp1252 consoles when the model is downloaded.
    """
    global _READER
    if _READER is None:
        import easyocr  # type: ignore
        _READER = easyocr.Reader(
            ["en"], gpu=False, recognizer=False, verbose=False
        )
    return _READER


def detect_blocks(
    image_path: str | Path | Image.Image,
    *,
    text_threshold: float = DEFAULT_TEXT_THRESHOLD,
    low_text: float = DEFAULT_LOW_TEXT,
    link_threshold: float = DEFAULT_LINK_THRESHOLD,
) -> list[BBox]:
    """Return axis-aligned text-block bounding boxes for the banner.

    Falls back to a single whole-image BBox if easyocr / torch is not
    available — keeps the rest of the pipeline runnable in environments
    without the heavy deps.
    """
    img = _load_image(image_path)
    arr = np.array(img.convert("RGB"))

    try:
        reader = _get_reader()
    except (ImportError, ModuleNotFoundError):
        h, w = arr.shape[:2]
        return [BBox(0, 0, w, h)]

    horizontal_list, free_list = reader.detect(
        arr,
        text_threshold=text_threshold,
        low_text=low_text,
        link_threshold=link_threshold,
    )

    boxes: list[BBox] = []
    # `horizontal_list` is a list of [[x_min, x_max, y_min, y_max], ...]
    # nested one level deeper than expected — flatten.
    for group in horizontal_list:
        for entry in group:
            x_min, x_max, y_min, y_max = entry
            boxes.append(BBox(
                x=int(x_min),
                y=int(y_min),
                w=int(x_max - x_min),
                h=int(y_max - y_min),
            ))

    # `free_list` carries rotated/quad polygons. For v1, approximate
    # with their axis-aligned bounding box.
    for group in free_list:
        for poly in group:
            arr_poly = np.array(poly)
            x_min, y_min = arr_poly.min(axis=0)
            x_max, y_max = arr_poly.max(axis=0)
            boxes.append(BBox(
                x=int(x_min),
                y=int(y_min),
                w=int(x_max - x_min),
                h=int(y_max - y_min),
            ))

    # Sort by reading order (top-to-bottom, then left-to-right) so
    # downstream block-vs-section matching is deterministic.
    boxes.sort(key=lambda b: (b.y, b.x))
    return boxes


def _load_image(image_path: str | Path | Image.Image) -> Image.Image:
    if isinstance(image_path, Image.Image):
        return image_path
    return Image.open(image_path)
