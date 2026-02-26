"""
Tests for google_batch_annotate_images and cache-injection integration.

Covers:
  1. 20-zone batch → 2 calls (16 + 4)
  2. Element failure (error.message set)
  3. Whole-batch exception → all results fallback
  4. Single-image path unchanged (cache miss → direct call)
  5. Cache injection: pre-computed result consumed by _ocr_google
  6. Non-Google engines unaffected (smoke test)
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import unittest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers to build mock Vision responses
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
    return resp


def _make_batch_resp(resps):
    batch = MagicMock()
    batch.responses = resps
    return batch


# ---------------------------------------------------------------------------
# Test: google_batch_annotate_images chunking
# ---------------------------------------------------------------------------

class TestGoogleBatchAnnotateImages(unittest.TestCase):

    def _make_fake_bytes(self, n):
        return [bytes([i % 256, i // 256]) for i in range(n)]

    @patch("app.ocr.vision")
    def test_20_images_two_batch_calls(self, mock_vision_module):
        """20 images → batch_annotate_images called twice: 16 + 4."""
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        mock_vision_module.ImageAnnotatorClient.return_value = mock_client
        mock_vision_module.Image.side_effect = lambda content: MagicMock()
        mock_vision_module.AnnotateImageRequest.side_effect = lambda **kw: MagicMock()
        mock_vision_module.Feature.Type.DOCUMENT_TEXT_DETECTION = "DTD"
        mock_vision_module.Feature.side_effect = lambda type_: MagicMock()

        # Both batch calls return 16 / 4 good responses
        resps_16 = [_make_resp(f"TEXT_{i}") for i in range(16)]
        resps_4 = [_make_resp(f"TEXT_{16+i}") for i in range(4)]
        mock_client.batch_annotate_images.side_effect = [
            _make_batch_resp(resps_16),
            _make_batch_resp(resps_4),
        ]

        images = self._make_fake_bytes(20)
        results = google_batch_annotate_images(images)

        self.assertEqual(mock_client.batch_annotate_images.call_count, 2)
        first_call_args = mock_client.batch_annotate_images.call_args_list[0]
        second_call_args = mock_client.batch_annotate_images.call_args_list[1]
        # Each call receives a list of requests
        first_requests = first_call_args.kwargs.get("requests") or first_call_args[1].get("requests") or first_call_args[0][0]
        second_requests = second_call_args.kwargs.get("requests") or second_call_args[1].get("requests") or second_call_args[0][0]
        self.assertEqual(len(first_requests), 16)
        self.assertEqual(len(second_requests), 4)
        self.assertEqual(len(results), 20)

    @patch("app.ocr.vision")
    def test_result_order_preserved(self, mock_vision_module):
        """Results must be in the same order as input."""
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        mock_vision_module.ImageAnnotatorClient.return_value = mock_client
        mock_vision_module.Image.side_effect = lambda content: MagicMock()
        mock_vision_module.AnnotateImageRequest.side_effect = lambda **kw: MagicMock()
        mock_vision_module.Feature.Type.DOCUMENT_TEXT_DETECTION = "DTD"
        mock_vision_module.Feature.side_effect = lambda type_: MagicMock()

        texts = [f"ZONE_{i}" for i in range(5)]
        resps = [_make_resp(t) for t in texts]
        mock_client.batch_annotate_images.return_value = _make_batch_resp(resps)

        results = google_batch_annotate_images(self._make_fake_bytes(5))
        self.assertEqual([r.text for r in results], texts)

    @patch("app.ocr.vision")
    def test_element_failure_returns_fallback(self, mock_vision_module):
        """Element with non-empty error.message returns empty OCRResult fallback."""
        from app.ocr import google_batch_annotate_images, OCRResult

        mock_client = MagicMock()
        mock_vision_module.ImageAnnotatorClient.return_value = mock_client
        mock_vision_module.Image.side_effect = lambda content: MagicMock()
        mock_vision_module.AnnotateImageRequest.side_effect = lambda **kw: MagicMock()
        mock_vision_module.Feature.Type.DOCUMENT_TEXT_DETECTION = "DTD"
        mock_vision_module.Feature.side_effect = lambda type_: MagicMock()

        good = _make_resp("OK", 0.9)
        bad = _make_resp(error_msg="vision error")  # element failure
        good2 = _make_resp("OK2", 0.8)
        mock_client.batch_annotate_images.return_value = _make_batch_resp([good, bad, good2])

        results = google_batch_annotate_images(self._make_fake_bytes(3))
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].text, "OK")
        self.assertEqual(results[1].text, "")          # fallback
        self.assertEqual(results[1].confidence, 0.0)  # fallback
        self.assertEqual(results[1].engine, "google")  # fallback
        self.assertEqual(results[2].text, "OK2")

    @patch("app.ocr.vision")
    def test_whole_batch_exception_all_fallback(self, mock_vision_module):
        """If batch_annotate_images raises, all elements in that chunk are fallbacks."""
        from app.ocr import google_batch_annotate_images

        mock_client = MagicMock()
        mock_vision_module.ImageAnnotatorClient.return_value = mock_client
        mock_vision_module.Image.side_effect = lambda content: MagicMock()
        mock_vision_module.AnnotateImageRequest.side_effect = lambda **kw: MagicMock()
        mock_vision_module.Feature.Type.DOCUMENT_TEXT_DETECTION = "DTD"
        mock_vision_module.Feature.side_effect = lambda type_: MagicMock()

        mock_client.batch_annotate_images.side_effect = RuntimeError("network error")

        results = google_batch_annotate_images(self._make_fake_bytes(3))
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertEqual(r.text, "")
            self.assertEqual(r.confidence, 0.0)
            self.assertEqual(r.engine, "google")

    @patch("app.ocr.vision")
    def test_empty_input_returns_empty(self, mock_vision_module):
        from app.ocr import google_batch_annotate_images
        results = google_batch_annotate_images([])
        self.assertEqual(results, [])
        mock_vision_module.ImageAnnotatorClient.assert_not_called()


# ---------------------------------------------------------------------------
# Test: cache injection (_google_cache_put / _ocr_google)
# ---------------------------------------------------------------------------

class TestGoogleCacheInjection(unittest.TestCase):

    def test_cached_result_consumed_by_ocr_google(self):
        """Pre-computed result in cache is returned by _ocr_google, no API call."""
        from app.ocr import _google_cache_put, _google_cache_pop, OCRResult
        import app.ocr as ocr_mod

        fake_bytes = b"fake_image_data_unique_XYZ"
        pre_result = OCRResult("PRE-COMPUTED", 0.77, "google")
        _google_cache_put(fake_bytes, pre_result)

        with patch.object(ocr_mod, "vision", None):  # ensure real API not called
            result = ocr_mod._ocr_google(fake_bytes)

        self.assertIsNotNone(result)
        self.assertEqual(result[0], "PRE-COMPUTED")
        self.assertEqual(result[1], 0.77)

    def test_cached_result_consumed_once(self):
        """Cache entry is removed after first read (consume-once semantics)."""
        from app.ocr import _google_cache_put, _google_cache_pop, OCRResult

        fake_bytes = b"consume_once_test"
        _google_cache_put(fake_bytes, OCRResult("RESULT", 0.9, "google"))
        first = _google_cache_pop(fake_bytes)
        second = _google_cache_pop(fake_bytes)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_fallback_cached_result_returns_none(self):
        """Cached fallback OCRResult (empty text, 0.0 conf) → _ocr_google returns None."""
        from app.ocr import _google_cache_put, OCRResult
        import app.ocr as ocr_mod

        fake_bytes = b"fallback_test_data"
        _google_cache_put(fake_bytes, OCRResult("", 0.0, "google"))

        with patch.object(ocr_mod, "vision", None):
            result = ocr_mod._ocr_google(fake_bytes)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Test: single-image path unchanged (cache miss → real API called)
# ---------------------------------------------------------------------------

class TestSingleImageFallback(unittest.TestCase):

    @patch("app.ocr.vision")
    def test_no_cache_calls_real_api(self, mock_vision_module):
        """With no cached result, _ocr_google falls through to single-image API."""
        from app.ocr import _google_cache_clear
        import app.ocr as ocr_mod

        fake_bytes = b"no_cache_here"
        # Ensure no stale cache entry
        _google_cache_clear([id(fake_bytes)])

        mock_client = MagicMock()
        mock_vision_module.ImageAnnotatorClient.return_value = mock_client
        mock_vision_module.Image.side_effect = lambda content: MagicMock()
        resp = MagicMock()
        resp.error.message = ""
        full = _make_full_text("DIRECT", 0.85)
        resp.full_text_annotation = full
        mock_client.document_text_detection.return_value = resp

        result = ocr_mod._ocr_google(fake_bytes)
        mock_client.document_text_detection.assert_called_once()
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "DIRECT")


# ---------------------------------------------------------------------------
# Test: Azure and OCR.Space unaffected (smoke)
# ---------------------------------------------------------------------------

class TestNonGoogleEnginesUnaffected(unittest.TestCase):

    def test_azure_not_called_by_google_batch(self):
        """google_batch_annotate_images never touches Azure path."""
        from app.ocr import google_batch_annotate_images
        with patch("app.ocr._ocr_azure") as mock_azure:
            with patch("app.ocr.vision") as mock_v:
                mock_client = MagicMock()
                mock_v.ImageAnnotatorClient.return_value = mock_client
                mock_v.Image.side_effect = lambda content: MagicMock()
                mock_v.AnnotateImageRequest.side_effect = lambda **kw: MagicMock()
                mock_v.Feature.Type.DOCUMENT_TEXT_DETECTION = "DTD"
                mock_v.Feature.side_effect = lambda type_: MagicMock()
                mock_client.batch_annotate_images.return_value = _make_batch_resp(
                    [_make_resp("T")]
                )
                google_batch_annotate_images([b"img"])
            mock_azure.assert_not_called()

    def test_run_ocr_multi_azure_path_unchanged(self):
        """run_ocr_multi for azure still calls _ocr_azure, not Google batch."""
        from app.ocr import run_ocr_multi
        with patch("app.ocr._ocr_azure", return_value=("AZURE_TEXT", 0.8)) as mock_azure:
            result = run_ocr_multi(b"img", ["azure"])
        mock_azure.assert_called_once_with(b"img")
        self.assertIn("azure", result)
        self.assertEqual(result["azure"].text, "AZURE_TEXT")


if __name__ == "__main__":
    unittest.main()
