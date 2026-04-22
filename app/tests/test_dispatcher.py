"""
Test suite for app/pipeline/ocr_dispatcher.py

Mandatory case: engine exception → error="engine_exception", no crash.
"""
import sys
import os
import hashlib
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.pipeline.models import ZoneDef
from app.pipeline.cropped_image import CroppedImage
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr


class TestDispatcher(unittest.TestCase):

    def _make_zone(self, engines):
        return ZoneDef(
            name="headline",
            type="ocr" if engines else "logo",
            bbox=[0, 0, 100, 100],
            engines=engines,
        )

    def _make_crop(self):
        return CroppedImage(
            bytes=b"crop",
            bbox=[1, 1, 10, 10],
            original_width=100,
            original_height=100,
            crop_width=9,
            crop_height=9,
            original_sha256="deadbeef",
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
                result = dispatch_zone_ocr(zone, self._make_crop())
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
        image_bytes = b"same"
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


if __name__ == "__main__":
    unittest.main()
