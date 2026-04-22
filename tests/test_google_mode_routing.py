# app/tests/test_google_mode_routing.py
import io
import struct
import zlib
import unittest
from unittest.mock import MagicMock, patch

from app.ocr import run_ocr_multi
from app.pipeline.cropped_image import CroppedImage
from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr


def _png_bytes(width: int, height: int, color=(9, 8, 7)) -> bytes:
    r, g, b = color
    row = b"\x00" + bytes([r, g, b]) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")



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

        crop_bytes = _png_bytes(19, 19)

        with patch(
            "app.pipeline.ocr_dispatcher.run_ocr_multi",
            return_value={"google": fake_result},
        ) as mock_run:
            dispatch_zone_ocr(
                zone,
                CroppedImage(
                    bytes=crop_bytes,
                    bbox=[1, 1, 20, 20],
                    original_width=100,
                    original_height=100,
                    crop_width=19,
                    crop_height=19,
                    original_sha256="a" * 64,
                    cropped=True,
                ),
            )

        mock_run.assert_called_once_with(crop_bytes, ["google"], {"google_mode": "text"})


if __name__ == "__main__":
    unittest.main()
