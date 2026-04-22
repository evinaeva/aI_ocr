"""
Test suite for app/pipeline/ocr_dispatcher.py

Mandatory case: engine exception → error="engine_exception", no crash.
"""
import hashlib
import io
import os
import struct
import sys
import unittest
import zlib
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.pipeline.cropped_image import CroppedImage
from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr


class TestDispatcher(unittest.TestCase):

    @staticmethod
    def _png_bytes(width: int, height: int, color=(1, 2, 3)) -> bytes:
        r, g, b = color
        row = b"\x00" + bytes([r, g, b]) * width
        raw = row * height

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

    def _make_zone(self, engines):
        return ZoneDef(
            name="headline",
            type="ocr" if engines else "logo",
            bbox=[0, 0, 100, 100],
            engines=engines,
        )

    def _make_crop(self):
        return CroppedImage(
            bytes=self._png_bytes(9, 9),
            bbox=[1, 1, 10, 10],
            original_width=100,
            original_height=100,
            crop_width=9,
            crop_height=9,
            original_sha256="d" * 64,
            cropped=True,
        )

    def test_empty_engines_returns_empty_list(self):
        zone = self._make_zone([])
        result = dispatch_zone_ocr(zone, self._make_crop())
        self.assertEqual(result, [])

    def test_engine_exception_produces_error_result(self):
        zone = self._make_zone(["google"])

        with patch("app.pipeline.ocr_dispatcher.run_ocr_multi") as mock_run:
            mock_run.side_effect = RuntimeError("simulated engine crash")
            result = dispatch_zone_ocr(zone, self._make_crop())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].engine, "google")
        self.assertEqual(result[0].error, "engine_exception")
        self.assertEqual(result[0].text, "")
        self.assertIsNone(result[0].confidence)

    def test_engine_exception_no_crash(self):
        """Verify dispatch_zone_ocr never raises even on exception."""
        zone = self._make_zone(["azure"])

        with patch("app.pipeline.ocr_dispatcher.run_ocr_multi") as mock_run:
            mock_run.side_effect = Exception("crash")
            try:
                dispatch_zone_ocr(zone, self._make_crop())
            except Exception:
                self.fail("dispatch_zone_ocr raised an exception")

    def test_successful_ocr(self):
        zone = self._make_zone(["google"])
        mock_ocr_result = MagicMock()
        mock_ocr_result.text = "Hello World"
        mock_ocr_result.confidence = 0.95

        with patch("app.pipeline.ocr_dispatcher.run_ocr_multi") as mock_run:
            mock_run.return_value = {"google": mock_ocr_result}
            result = dispatch_zone_ocr(zone, self._make_crop())

        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].error)
        self.assertEqual(result[0].text, "Hello World")
        self.assertEqual(result[0].confidence, 0.95)

    def test_cropped_image_rejects_identical_original_bytes(self):
        image_bytes = self._png_bytes(1, 1)
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=image_bytes,
                bbox=[0, 0, 1, 1],
                original_width=10,
                original_height=10,
                crop_width=1,
                crop_height=1,
                original_sha256=hashlib.sha256(image_bytes).hexdigest(),
                cropped=True,
            )

    def test_dispatcher_rejects_missing_original_sha256_defensively(self):
        zone = self._make_zone(["google"])
        forged = object.__new__(CroppedImage)
        object.__setattr__(forged, "bytes", self._png_bytes(9, 9))
        object.__setattr__(forged, "bbox", [1, 1, 10, 10])
        object.__setattr__(forged, "original_width", 100)
        object.__setattr__(forged, "original_height", 100)
        object.__setattr__(forged, "crop_width", 9)
        object.__setattr__(forged, "crop_height", 9)
        object.__setattr__(forged, "original_sha256", "")
        object.__setattr__(forged, "cropped", True)

        with self.assertRaises(RuntimeError):
            dispatch_zone_ocr(zone, forged)


if __name__ == "__main__":
    unittest.main()
