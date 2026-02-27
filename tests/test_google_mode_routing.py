# app/tests/test_google_mode_routing.py
import unittest
from unittest.mock import MagicMock, patch

from app.ocr import run_ocr_multi
from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr


class TestGoogleModeRouting(unittest.TestCase):
    def test_run_ocr_multi_passes_google_mode_from_engine_config(self):
        mock_google = MagicMock(return_value=("TXT", 0.9))

        # Patch dispatch map entry, because runtime calls via _ENGINE_FNS.
        with patch.dict("app.ocr._ENGINE_FNS", {"google": mock_google}, clear=False):
            run_ocr_multi(b"img", ["google"], {"google_mode": "document"})

        mock_google.assert_called_once_with(b"img", "document")

    def test_run_ocr_multi_passes_none_when_engine_config_missing(self):
        mock_google = MagicMock(return_value=("TXT", 0.9))

        with patch.dict("app.ocr._ENGINE_FNS", {"google": mock_google}, clear=False):
            run_ocr_multi(b"img", ["google"])

        # Contract in current implementation: google fn called with (image_bytes, google_mode)
        # and google_mode is None when engine_config missing.
        mock_google.assert_called_once_with(b"img", None)

    def test_dispatcher_forwards_zone_engine_config_to_run_ocr_multi(self):
        zone = ZoneDef(
            name="headline",
            type="ocr",
            bbox=[0, 0, 100, 100],
            engines=["google"],
            engine_config={"google_mode": "text"},
        )

        fake_result = type("R", (), {"text": "ok", "confidence": 0.9})()

        with patch(
            "app.pipeline.ocr_dispatcher.run_ocr_multi",
            return_value={"google": fake_result},
        ) as mock_run:
            dispatch_zone_ocr(zone, b"img")

        mock_run.assert_called_once_with(b"img", ["google"], {"google_mode": "text"})


if __name__ == "__main__":
    unittest.main()
