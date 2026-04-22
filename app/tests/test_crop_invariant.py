import hashlib
import io
import os
import struct
import unittest
import zlib

os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app.pipeline.cropped_image import CroppedImage
from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr


class TestCropInvariant(unittest.TestCase):
    @staticmethod
    def _png_bytes(width: int, height: int, color=(12, 34, 56)) -> bytes:
        r, g, b = color
        row = b"\x00" + bytes([r, g, b]) * width
        raw = row * height

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

    def test_dispatch_rejects_raw_bytes_payload(self):
        zone = ZoneDef(name="z", type="ocr", bbox=[0, 0, 10, 10], engines=["google"])
        with self.assertRaises(RuntimeError):
            dispatch_zone_ocr(zone, b"raw-image-bytes")  # type: ignore[arg-type]

    def test_cropped_image_rejects_missing_bbox(self):
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=self._png_bytes(10, 10),
                bbox=None,  # type: ignore[arg-type]
                original_width=100,
                original_height=100,
                crop_width=10,
                crop_height=10,
                original_sha256="f" * 64,
                cropped=True,
            )

    def test_cropped_image_rejects_missing_original_sha256(self):
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=self._png_bytes(10, 10),
                bbox=[1, 1, 11, 11],
                original_width=100,
                original_height=100,
                crop_width=10,
                crop_height=10,
                original_sha256="",
                cropped=True,
            )

    def test_cropped_image_rejects_zero_area_bbox(self):
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=self._png_bytes(1, 8),
                bbox=[2, 2, 2, 10],
                original_width=100,
                original_height=100,
                crop_width=1,
                crop_height=8,
                original_sha256="a" * 64,
                cropped=True,
            )

    def test_cropped_image_rejects_full_image_bbox_geometry(self):
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=self._png_bytes(1000, 1000),
                bbox=[0, 0, 1000, 1000],
                original_width=1000,
                original_height=1000,
                crop_width=1000,
                crop_height=1000,
                original_sha256="a" * 64,
                cropped=True,
            )

    def test_cropped_image_rejects_forged_full_payload(self):
        full_bytes = self._png_bytes(1000, 1000)
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=full_bytes,
                bbox=[1, 1, 2, 2],
                original_width=1000,
                original_height=1000,
                crop_width=1,
                crop_height=1,
                original_sha256="b" * 64,
                cropped=True,
            )

    def test_cropped_image_rejects_decoded_dimensions_mismatch(self):
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=self._png_bytes(100, 100),
                bbox=[10, 10, 20, 20],
                original_width=1000,
                original_height=1000,
                crop_width=10,
                crop_height=10,
                original_sha256="c" * 64,
                cropped=True,
            )

    def test_cropped_image_rejects_near_full_area_ratio_when_env_is_weaker(self):
        os.environ["OCR_MAX_CROP_AREA_RATIO"] = "1.0"
        try:
            with self.assertRaises(RuntimeError):
                CroppedImage(
                    bytes=self._png_bytes(975, 975),
                    bbox=[0, 0, 975, 975],
                    original_width=1000,
                    original_height=1000,
                    crop_width=975,
                    crop_height=975,
                    original_sha256="d" * 64,
                    cropped=True,
                )
        finally:
            os.environ.pop("OCR_MAX_CROP_AREA_RATIO", None)

    def test_cropped_image_rejects_92_percent_when_env_stricter(self):
        os.environ["OCR_MAX_CROP_AREA_RATIO"] = "0.90"
        try:
            with self.assertRaises(RuntimeError):
                CroppedImage(
                    bytes=self._png_bytes(920, 1000),
                    bbox=[0, 0, 920, 1000],
                    original_width=1000,
                    original_height=1000,
                    crop_width=920,
                    crop_height=1000,
                    original_sha256="e" * 64,
                    cropped=True,
                )
        finally:
            os.environ.pop("OCR_MAX_CROP_AREA_RATIO", None)

    def test_cropped_image_accepts_reasonable_subset_crop(self):
        crop = self._png_bytes(500, 400)
        accepted = CroppedImage(
            bytes=crop,
            bbox=[100, 100, 600, 500],
            original_width=1000,
            original_height=1000,
            crop_width=500,
            crop_height=400,
            original_sha256=hashlib.sha256(self._png_bytes(1000, 1000)).hexdigest(),
            cropped=True,
        )
        self.assertEqual(accepted.crop_width, 500)


if __name__ == "__main__":
    unittest.main()
