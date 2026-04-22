from __future__ import annotations

import binascii
import hashlib
import os
import struct
from dataclasses import dataclass
from typing import List


HARD_MAX_CROP_AREA_RATIO = 0.95


@dataclass(frozen=True)
class CroppedImage:
    bytes: bytes
    bbox: List[int]
    original_width: int
    original_height: int
    crop_width: int
    crop_height: int
    original_sha256: str
    cropped: bool = True

    @staticmethod
    def _effective_max_ratio() -> float:
        ratio_raw = os.getenv("OCR_MAX_CROP_AREA_RATIO", str(HARD_MAX_CROP_AREA_RATIO)).strip()
        try:
            configured = float(ratio_raw)
        except ValueError:
            configured = HARD_MAX_CROP_AREA_RATIO

        if configured <= 0.0:
            return HARD_MAX_CROP_AREA_RATIO
        return min(configured, HARD_MAX_CROP_AREA_RATIO)

    @staticmethod
    def _decode_png_size(payload: bytes) -> tuple[int, int] | None:
        signature = b"\x89PNG\r\n\x1a\n"
        if len(payload) < 24 or payload[:8] != signature:
            return None
        if payload[12:16] != b"IHDR":
            return None
        width = struct.unpack(">I", payload[16:20])[0]
        height = struct.unpack(">I", payload[20:24])[0]
        return (width, height)

    @staticmethod
    def _decode_gif_size(payload: bytes) -> tuple[int, int] | None:
        if len(payload) < 10 or payload[:6] not in (b"GIF87a", b"GIF89a"):
            return None
        width = struct.unpack("<H", payload[6:8])[0]
        height = struct.unpack("<H", payload[8:10])[0]
        return (width, height)

    @staticmethod
    def _decode_jpeg_size(payload: bytes) -> tuple[int, int] | None:
        if len(payload) < 4 or payload[0:2] != b"\xff\xd8":
            return None
        idx = 2
        while idx + 9 < len(payload):
            if payload[idx] != 0xFF:
                idx += 1
                continue
            marker = payload[idx + 1]
            idx += 2
            if marker in (0xD8, 0xD9):
                continue
            if idx + 2 > len(payload):
                break
            seg_len = struct.unpack(">H", payload[idx:idx + 2])[0]
            if seg_len < 2 or idx + seg_len > len(payload):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if seg_len < 7:
                    break
                height = struct.unpack(">H", payload[idx + 3:idx + 5])[0]
                width = struct.unpack(">H", payload[idx + 5:idx + 7])[0]
                return (width, height)
            idx += seg_len
        return None

    @classmethod
    def _decoded_size(cls, payload: bytes) -> tuple[int, int]:
        for decoder in (cls._decode_png_size, cls._decode_gif_size, cls._decode_jpeg_size):
            out = decoder(payload)
            if out is not None:
                return out
        raise RuntimeError("Invalid crop bytes — refusing OCR")

    def validate_for_ocr(self) -> None:
        if self.bbox is None:
            raise RuntimeError("Missing bbox — refusing OCR without crop bounds")
        if not isinstance(self.bbox, (list, tuple)):
            raise RuntimeError("Invalid bbox — expected [x1, y1, x2, y2]")
        if len(self.bbox) != 4:
            raise RuntimeError("Invalid bbox — expected [x1, y1, x2, y2]")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in self.bbox):
            raise RuntimeError("Invalid bbox — expected integer coordinates")

        x1, y1, x2, y2 = self.bbox
        if x2 <= x1 or y2 <= y1:
            raise RuntimeError("Zero-area crop — refusing OCR")

        if self.original_width <= 0 or self.original_height <= 0:
            raise RuntimeError("Invalid original dimensions — refusing OCR")
        if self.crop_width <= 0 or self.crop_height <= 0:
            raise RuntimeError("Invalid crop dimensions — refusing OCR")

        expected_crop_width = x2 - x1
        expected_crop_height = y2 - y1
        if self.crop_width != expected_crop_width:
            raise RuntimeError("Crop width mismatch — refusing OCR")
        if self.crop_height != expected_crop_height:
            raise RuntimeError("Crop height mismatch — refusing OCR")

        if self.crop_width > self.original_width or self.crop_height > self.original_height:
            raise RuntimeError("Crop exceeds original dimensions — refusing OCR")
        if not self.bytes:
            raise RuntimeError("Empty crop bytes — refusing OCR")
        if not self.cropped:
            raise RuntimeError("Unmarked payload — refusing OCR")

        actual_w, actual_h = self._decoded_size(self.bytes)
        if actual_w != self.crop_width:
            raise RuntimeError("Decoded crop width mismatch — refusing OCR")
        if actual_h != self.crop_height:
            raise RuntimeError("Decoded crop height mismatch — refusing OCR")

        if x1 <= 0 and y1 <= 0 and x2 >= self.original_width and y2 >= self.original_height:
            raise RuntimeError("Crop spans full image edges — refusing OCR")

        crop_area = float(self.crop_width * self.crop_height)
        original_area = float(self.original_width * self.original_height)
        if original_area <= 0:
            raise RuntimeError("Invalid original area — refusing OCR")

        max_ratio = self._effective_max_ratio()
        if (crop_area / original_area) >= max_ratio:
            raise RuntimeError("Crop too large — refusing near-full-image OCR")

        if not isinstance(self.original_sha256, str) or not self.original_sha256.strip():
            raise RuntimeError("Missing original hash — refusing OCR")
        if len(self.original_sha256.strip()) != 64:
            raise RuntimeError("Invalid original hash format — refusing OCR")
        try:
            binascii.unhexlify(self.original_sha256.strip())
        except (binascii.Error, ValueError) as exc:
            raise RuntimeError("Invalid original hash format — refusing OCR") from exc

        crop_sha = hashlib.sha256(self.bytes).hexdigest()
        if crop_sha == self.original_sha256:
            raise RuntimeError("Crop identical to original image — refusing OCR")

    def __post_init__(self) -> None:
        self.validate_for_ocr()
