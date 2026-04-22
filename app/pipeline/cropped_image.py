from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class CroppedImage:
    bytes: bytes
    bbox: List[int]
    original_width: int
    original_height: int
    crop_width: int
    crop_height: int
    original_sha256: Optional[str] = None
    cropped: bool = True

    def __post_init__(self) -> None:
        if self.bbox is None:
            raise RuntimeError("Missing bbox — refusing OCR without crop bounds")
        if len(self.bbox) != 4:
            raise RuntimeError("Invalid bbox — expected [x1, y1, x2, y2]")
        x1, y1, x2, y2 = self.bbox
        if x2 <= x1 or y2 <= y1:
            raise RuntimeError("Zero-area crop — refusing OCR")
        if self.original_width <= 0 or self.original_height <= 0:
            raise RuntimeError("Invalid original dimensions — refusing OCR")
        if self.crop_width <= 0 or self.crop_height <= 0:
            raise RuntimeError("Invalid crop dimensions — refusing OCR")
        if self.crop_width > self.original_width or self.crop_height > self.original_height:
            raise RuntimeError("Crop exceeds original dimensions — refusing OCR")
        if not self.bytes:
            raise RuntimeError("Empty crop bytes — refusing OCR")
        if not self.cropped:
            raise RuntimeError("Unmarked payload — refusing OCR")
        if x1 <= 0 and y1 <= 0 and x2 >= self.original_width and y2 >= self.original_height:
            raise RuntimeError("Crop spans full image edges — refusing OCR")

        ratio_raw = os.getenv("OCR_MAX_CROP_AREA_RATIO", "0.95").strip()
        try:
            max_ratio = float(ratio_raw)
        except ValueError:
            max_ratio = 0.95
        if max_ratio <= 0.0 or max_ratio > 1.0:
            max_ratio = 0.95
        crop_area = float(self.crop_width * self.crop_height)
        original_area = float(self.original_width * self.original_height)
        if original_area <= 0:
            raise RuntimeError("Invalid original area — refusing OCR")
        if (crop_area / original_area) >= max_ratio:
            raise RuntimeError("Crop too large — refusing near-full-image OCR")

        if self.original_sha256:
            crop_sha = hashlib.sha256(self.bytes).hexdigest()
            if crop_sha == self.original_sha256:
                raise RuntimeError("Crop identical to original image — refusing OCR")
