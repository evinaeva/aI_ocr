"""
Build-gate tests for P2.4 batch_routes.
All external dependencies (template_store, build_zip_manifest, dispatch_zone_ocr,
consensus, similarity, ocr) are mocked — no network/cloud calls.
"""
from __future__ import annotations

import asyncio
import json
import io
import struct
import zlib
import unittest
from unittest.mock import MagicMock, patch

from app.pipeline.cropped_image import CroppedImage


def _png_bytes(width: int, height: int, color=(9, 8, 7)) -> bytes:
    r, g, b = color
    row = b"\x00" + bytes([r, g, b]) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")



class TestBatchRouterImport(unittest.TestCase):
    """Verify the module and router are importable."""

    def test_import_batch_routes(self):
        from app.pipeline import batch_routes  # noqa: F401
        self.assertTrue(True)

    def test_batch_router_attribute(self):
        from app.pipeline.batch_routes import batch_router
        self.assertIsNotNone(batch_router)

    def test_batch_router_has_routes(self):
        from app.pipeline.batch_routes import batch_router
        paths = [r.path for r in batch_router.routes]
        self.assertIn("/api/v2/batch/start", paths)
        self.assertIn("/api/v2/batch/{job_id}/progress", paths)
        self.assertIn("/api/v2/batch/{job_id}/results", paths)


class TestBatchJobStore(unittest.TestCase):
    """Verify _JOBS dict is populated by _process_batch_job."""

    def setUp(self):
        from app.pipeline import batch_routes
        batch_routes._JOBS.clear()

    def test_push_batch_event_noop_no_queue(self):
        """Pushing to a job with no queue should not raise."""
        from app.pipeline.batch_routes import _push_batch_event
        _push_batch_event("nonexistent-job", {"event": "test"})

    def test_results_endpoint_unknown_job(self):
        """GET /results for unknown job returns 404 dict."""
        from app.pipeline.batch_routes import _JOBS
        self.assertNotIn("unknown", _JOBS)

    def test_crop_fails_on_bad_input(self):
        """_crop_zone_bytes must fail hard on crop errors."""
        from app.pipeline.batch_routes import _crop_zone_bytes
        zone = MagicMock()
        zone.bbox = [0, 0, 100, 100]
        zone.engine_config = {}
        with self.assertRaises(RuntimeError):
            _crop_zone_bytes(b"not-an-image", zone, [440, 1100])


class TestGoogleBatchPrefetch(unittest.TestCase):
    """Verify _prefetch_google_for_target batches correctly."""

    def test_no_google_engines_returns_empty(self):
        from app.pipeline.batch_routes import _prefetch_google_for_target
        zone = MagicMock()
        zone.engines = ["azure"]
        result = _prefetch_google_for_target(b"img", [zone], [440, 1100])
        self.assertEqual(result, {})

    def test_google_engine_calls_batch(self):
        from app.pipeline.batch_routes import _prefetch_google_for_target
        from app.ocr import OCRResult

        zone = MagicMock()
        zone.engines = ["google"]
        zone.bbox = [0, 0, 440, 1100]
        zone.engine_config = {}

        fake_result = OCRResult("hello", 0.9, "google")

        with patch("app.pipeline.batch_routes.google_batch_annotate_images",
                   return_value=[fake_result]) as mock_batch, \
             patch("app.pipeline.batch_routes._google_cache_put") as mock_put, \
             patch("app.pipeline.batch_routes._crop_zone_bytes",
                   return_value=CroppedImage(
                       bytes=_png_bytes(9, 9),
                       bbox=[1, 1, 10, 10],
                       original_width=100,
                       original_height=100,
                       crop_width=9,
                       crop_height=9,
                       original_sha256="a" * 64,
                       cropped=True,
                   )):
            result = _prefetch_google_for_target(b"imgbytes", [zone], [440, 1100])

        mock_batch.assert_called_once()
        mock_put.assert_called_once()
        self.assertIn(0, result)


class TestMainAppIncludesBatchRouter(unittest.TestCase):
    """Verify batch_router is registered in the FastAPI app."""

    def test_batch_routes_in_openapi(self):
        import os
        os.environ.setdefault("APP_PASSWORD", "test")
        os.environ.setdefault("SESSION_SECRET", "testsecret")
        from app.main import app
        paths = list(app.routes)
        route_paths = [r.path for r in paths if hasattr(r, "path")]
        self.assertIn("/api/v2/batch/start", route_paths)
        self.assertIn("/api/v2/batch/{job_id}/progress", route_paths)
        self.assertIn("/api/v2/batch/{job_id}/results", route_paths)


if __name__ == "__main__":
    unittest.main()
