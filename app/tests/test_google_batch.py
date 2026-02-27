"""
Tests for google_batch_annotate_images and cache-injection integration.

Covers:
  1. 20-zone batch → batch_annotate_images called twice (16 + 4)
  2. Result order preserved
  3. Element failure (error.message set) → fallback OCRResult
  4. Whole-batch exception → all fallback
  5. Cache injection: pre-computed result consumed by _ocr_google
  6. Cache consume-once semantics
  7. Fallback cached result (empty/0.0) → _ocr_google returns None
  8. Cache miss → real single-image API called
  9. Non-Google engines unaffected (smoke)
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Response-building helpers
# ---------------------------------------------------------------------------

def _make_full_text(text="HELLO", confidence=0.9):
    block = MagicMock()
    block.confidence = confidence
    page = MagicMock()
    page.blocks = [block]
    full = MagicMock()
    full.text = text
    full.pages = [page]
    return full


def _make_resp(text="HELLO", confidence=0.9, error_msg=""):
    resp = MagicMock()
    resp.error.message = error_msg
    resp.full_text_annotation = _make_full_text(text, confidence)
    ta = MagicMock()
    ta.description = text
    resp.text_annotations = [ta]
    return resp


def _make_batch_resp(resps):
    batch = MagicMock()
    batch.responses = resps
    return batch


def _fake_bytes(n):
    """Return n distinct bytes objects."""
    return [bytes([i % 256, i // 256, 0xAB]) for i in range(n)]


# ---------------------------------------------------------------------------
# Patch helper: replaces the lazy `from google.cloud import vision` inside
# google_batch_annotate_images by pre-setting the module attribute.
# ---------------------------------------------------------------------------

class _VisionPatch:
    """Context manager that injects a mock vision module into app.ocr."""

    def __init__(self, mock_client):
        self.mock_client = mock_client
        self._patcher = None

    def __enter__(self):
        import app.ocr as ocr_mod
        import importlib
        import types

        # Build a minimal fake vision module
        fake_vision = types.ModuleType("google.cloud.vision")
        fake_vision.ImageAnnotatorClient = MagicMock(return_value=self.mock_client)
        fake_vision.Image = MagicMock(side_effect=lambda content: MagicMock())
        fake_vision.AnnotateImageRequest = MagicMock(side_effect=lambda **kw: MagicMock())
        feature_type = MagicMock()
        feature_type.TEXT_DETECTION = "TD"
        feature_type.DOCUMENT_TEXT_DETECTION = "DTD"
        fake_feature = MagicMock()
        fake_feature.Type = feature_type
        fake_feature.side_effect = lambda type_: MagicMock()
        fake_vision.Feature = fake_feature

        # Patch sys.modules so lazy imports inside functions pick it up
        import sys
        sys.modules["google"] = types.ModuleType("google")
        sys.modules["google.cloud"] = types.ModuleType("google.cloud")
        sys.modules["google.cloud.vision"] = fake_vision
        # Also set the cloud sub-namespace attribute
        sys.modules["google.cloud"].vision = fake_vision
        sys.modules["google"].cloud = sys.modules["google.cloud"]

        self._fake_vision = fake_vision
        return fake_vision

    def __exit__(self, *args):
        import sys
        for key in ["google.cloud.vision", "google.cloud", "google"]:
            sys.modules.pop(key, None)


# ---------------------------------------------------------------------------
# 1 & 2: Chunking and order
# ---------------------------------------------------------------------------

class TestGoogleBatchChunking(unittest.TestCase):

    def test_20_images_two_batch_calls_16_plus_4(self):
        """20 images → batch_annotate_images called exactly twice: 16 then 4."""
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        resps_16 = [_make_resp(f"T{i}") for i in range(16)]
        resps_4 = [_make_resp(f"T{16+i}") for i in range(4)]
        mock_client.batch_annotate_images.side_effect = [
            _make_batch_resp(resps_16),
            _make_batch_resp(resps_4),
        ]

        with _VisionPatch(mock_client):
            results = google_batch_annotate_images(_fake_bytes(20))

        self.assertEqual(mock_client.batch_annotate_images.call_count, 2)
        # First call: 16 requests
        first_args = mock_client.batch_annotate_images.call_args_list[0]
        reqs_1 = (first_args.kwargs.get("requests")
                  or (first_args[1] or {}).get("requests")
                  or first_args[0][0])
        # Second call: 4 requests
        second_args = mock_client.batch_annotate_images.call_args_list[1]
        reqs_2 = (second_args.kwargs.get("requests")
                  or (second_args[1] or {}).get("requests")
                  or second_args[0][0])
        self.assertEqual(len(reqs_1), 16)
        self.assertEqual(len(reqs_2), 4)
        self.assertEqual(len(results), 20)

    def test_result_order_preserved(self):
        """Results are in the same order as inputs."""
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        texts = [f"ZONE_{i}" for i in range(5)]
        resps = [_make_resp(t) for t in texts]
        mock_client.batch_annotate_images.return_value = _make_batch_resp(resps)

        with _VisionPatch(mock_client):
            results = google_batch_annotate_images(_fake_bytes(5))

        self.assertEqual([r.text for r in results], texts)


# ---------------------------------------------------------------------------
# 3 & 4: Error handling
# ---------------------------------------------------------------------------

class TestGoogleBatchErrorHandling(unittest.TestCase):

    def test_element_failure_returns_fallback(self):
        """Element with non-empty error.message becomes empty fallback OCRResult."""
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        good = _make_resp("OK", 0.9)
        bad = _make_resp(error_msg="vision api error")
        good2 = _make_resp("OK2", 0.8)
        mock_client.batch_annotate_images.return_value = _make_batch_resp([good, bad, good2])

        with _VisionPatch(mock_client):
            results = google_batch_annotate_images(_fake_bytes(3))

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].text, "OK")
        self.assertEqual(results[1].text, "")           # fallback
        self.assertEqual(results[1].confidence, 0.0)   # fallback
        self.assertEqual(results[1].engine, "google")  # fallback
        self.assertEqual(results[2].text, "OK2")

    def test_whole_batch_exception_all_fallback(self):
        """If batch_annotate_images raises, all elements become fallbacks."""
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        mock_client.batch_annotate_images.side_effect = RuntimeError("network error")

        with _VisionPatch(mock_client):
            results = google_batch_annotate_images(_fake_bytes(3))

        self.assertEqual(len(results), 3)
        for r in results:
            self.assertEqual(r.text, "")
            self.assertEqual(r.confidence, 0.0)
            self.assertEqual(r.engine, "google")

    def test_empty_input_returns_empty_list(self):
        from app.ocr import google_batch_annotate_images
        mock_client = MagicMock()
        with _VisionPatch(mock_client):
            results = google_batch_annotate_images([])
        self.assertEqual(results, [])
        mock_client.batch_annotate_images.assert_not_called()


# ---------------------------------------------------------------------------
# 5, 6, 7: Cache injection
# ---------------------------------------------------------------------------

class TestGoogleCacheInjection(unittest.TestCase):

    def test_cached_result_consumed_by_ocr_google(self):
        """Pre-computed cache result returned by _ocr_google, no API call made."""
        import app.ocr as ocr_mod
        from app.ocr import _google_cache_put, OCRResult

        fake_bytes = b"unique_image_CACHE_HIT_XYZ"
        pre_result = OCRResult("PRE-COMPUTED", 0.77, "google")
        _google_cache_put(fake_bytes, pre_result)

        called = []
        original_init = ocr_mod._ocr_google  # save

        # Temporarily break real API path to confirm it is NOT reached
        import sys
        sys.modules.pop("google", None)
        sys.modules.pop("google.cloud", None)
        sys.modules.pop("google.cloud.vision", None)

        result = ocr_mod._ocr_google(fake_bytes)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "PRE-COMPUTED")
        self.assertEqual(result[1], 0.77)

    def test_cached_result_consumed_once(self):
        """Cache entry is removed after first read (consume-once)."""
        from app.ocr import _google_cache_put, _google_cache_pop, OCRResult

        fake_bytes = b"consume_once_uniq_1234"
        _google_cache_put(fake_bytes, OCRResult("RESULT", 0.9, "google"))
        first = _google_cache_pop(fake_bytes)
        second = _google_cache_pop(fake_bytes)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_fallback_cached_result_returns_none(self):
        """Cached fallback (empty text + 0.0 conf) causes _ocr_google to return None."""
        import app.ocr as ocr_mod
        from app.ocr import _google_cache_put, OCRResult
        import sys
        sys.modules.pop("google", None)
        sys.modules.pop("google.cloud", None)
        sys.modules.pop("google.cloud.vision", None)

        fake_bytes = b"fallback_test_unique_5678"
        _google_cache_put(fake_bytes, OCRResult("", 0.0, "google"))

        result = ocr_mod._ocr_google(fake_bytes)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 8: Cache miss → single-image path
# ---------------------------------------------------------------------------

class TestSingleImageCacheMiss(unittest.TestCase):

    def test_no_cache_calls_single_image_api(self):
        """With no cached result, _ocr_google uses single-image text_detection."""
        import app.ocr as ocr_mod
        from app.ocr import _google_cache_clear

        fake_bytes = b"no_cache_miss_unique_9999"
        _google_cache_clear([id(fake_bytes)])

        mock_client = MagicMock()
        resp = MagicMock()
        resp.error.message = ""
        resp.text_annotations = [MagicMock(description="DIRECT")]
        mock_client.text_detection.return_value = resp

        with _VisionPatch(mock_client):
            result = ocr_mod._ocr_google(fake_bytes)

        mock_client.text_detection.assert_called_once()
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "DIRECT")

    def test_document_mode_calls_document_text_detection(self):
        import app.ocr as ocr_mod
        from app.ocr import _google_cache_clear

        fake_bytes = b"doc_mode_unique_10000"
        _google_cache_clear([id(fake_bytes)])

        mock_client = MagicMock()
        resp = MagicMock()
        resp.error.message = ""
        resp.full_text_annotation = _make_full_text("DOC", 0.91)
        mock_client.document_text_detection.return_value = resp

        with _VisionPatch(mock_client):
            result = ocr_mod._ocr_google(fake_bytes, "document")

        mock_client.document_text_detection.assert_called_once()
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "DOC")


class TestGoogleBatchModes(unittest.TestCase):

    def test_batch_text_mode_uses_text_feature_and_text_annotations(self):
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        mock_client.batch_annotate_images.return_value = _make_batch_resp([_make_resp("TXT")])

        with _VisionPatch(mock_client) as fake_vision:
            results = google_batch_annotate_images([b"img"], google_mode="text")
            reqs = mock_client.batch_annotate_images.call_args.kwargs["requests"]
            self.assertEqual(len(reqs), 1)
            feature_arg = fake_vision.Feature.call_args.kwargs["type_"]
            self.assertEqual(feature_arg, fake_vision.Feature.Type.TEXT_DETECTION)

        self.assertEqual(results[0].text, "TXT")

    def test_batch_document_mode_uses_document_feature_and_full_text(self):
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        mock_client.batch_annotate_images.return_value = _make_batch_resp([_make_resp("DOC")])

        with _VisionPatch(mock_client) as fake_vision:
            results = google_batch_annotate_images([b"img"], google_mode="document")
            reqs = mock_client.batch_annotate_images.call_args.kwargs["requests"]
            self.assertEqual(len(reqs), 1)
            feature_arg = fake_vision.Feature.call_args.kwargs["type_"]
            self.assertEqual(feature_arg, fake_vision.Feature.Type.DOCUMENT_TEXT_DETECTION)

        self.assertEqual(results[0].text, "DOC")


# ---------------------------------------------------------------------------
# 9: Non-Google engines unaffected
# ---------------------------------------------------------------------------

class TestNonGoogleEnginesUnaffected(unittest.TestCase):

    def test_azure_not_called_by_google_batch(self):
        """google_batch_annotate_images never touches _ocr_azure."""
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        mock_client.batch_annotate_images.return_value = _make_batch_resp([_make_resp("T")])

        with patch("app.ocr._ocr_azure") as mock_azure:
            with _VisionPatch(mock_client):
                google_batch_annotate_images([b"img"])
        mock_azure.assert_not_called()

    def test_run_ocr_multi_azure_path_unchanged(self):
        """run_ocr_multi for azure calls _ocr_azure directly, not batch."""
        from app.ocr import run_ocr_multi
        mock_azure = MagicMock(return_value=("AZURE_TEXT", 0.8))
        with patch.dict("app.ocr._ENGINE_FNS", {"azure": mock_azure}, clear=False):
            result = run_ocr_multi(b"img", ["azure"])
        mock_azure.assert_called_once_with(b"img")
        self.assertIn("azure", result)
        self.assertEqual(result["azure"].text, "AZURE_TEXT")

    def test_run_ocr_multi_ocrspace_path_unchanged(self):
        """run_ocr_multi for ocrspace calls _ocr_ocrspace directly."""
        from app.ocr import run_ocr_multi
        mock_space = MagicMock(return_value=("SPACE_TEXT", 0.75))
        with patch.dict("app.ocr._ENGINE_FNS", {"ocrspace": mock_space}, clear=False):
            result = run_ocr_multi(b"img", ["ocrspace"])
        mock_space.assert_called_once_with(b"img")
        self.assertIn("ocrspace", result)
        self.assertEqual(result["ocrspace"].text, "SPACE_TEXT")


if __name__ == "__main__":
    unittest.main()
