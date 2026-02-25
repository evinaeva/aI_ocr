"""
Test suite for app/pipeline/ocr_dispatcher.py

Mandatory case: engine exception → error="engine_exception", no crash.
"""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr


class TestDispatcher(unittest.TestCase):

    def _make_zone(self, engines):
        return ZoneDef(
            name="headline",
            type="ocr" if engines else "logo",
            bbox=[0, 0, 100, 100],
            engines=engines,
        )

    def test_empty_engines_returns_empty_list(self):
        zone = self._make_zone([])
        result = dispatch_zone_ocr(zone, b"fake_image")
        self.assertEqual(result, [])

    def test_engine_exception_produces_error_result(self):
        zone = self._make_zone(["google"])

        with patch("app.pipeline.ocr_dispatcher.run_ocr_multi") as mock_run:
            mock_run.side_effect = RuntimeError("simulated engine crash")
            result = dispatch_zone_ocr(zone, b"fake_image")

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
                result = dispatch_zone_ocr(zone, b"fake_image")
            except Exception:
                self.fail("dispatch_zone_ocr raised an exception")

    def test_successful_ocr(self):
        zone = self._make_zone(["google"])
        mock_ocr_result = MagicMock()
        mock_ocr_result.text = "Hello World"
        mock_ocr_result.confidence = 0.95

        with patch("app.pipeline.ocr_dispatcher.run_ocr_multi") as mock_run:
            mock_run.return_value = {"google": mock_ocr_result}
            result = dispatch_zone_ocr(zone, b"fake_image")

        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].error)
        self.assertEqual(result[0].text, "Hello World")
        self.assertEqual(result[0].confidence, 0.95)


if __name__ == "__main__":
    unittest.main()
