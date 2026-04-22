import os
import unittest

os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app.pipeline.cropped_image import CroppedImage
from app.pipeline.models import ZoneDef
from app.pipeline.ocr_dispatcher import dispatch_zone_ocr


class TestCropInvariant(unittest.TestCase):
    def test_dispatch_rejects_raw_bytes_payload(self):
        zone = ZoneDef(name="z", type="ocr", bbox=[0, 0, 10, 10], engines=["google"])
        with self.assertRaises(RuntimeError):
            dispatch_zone_ocr(zone, b"raw-image-bytes")  # type: ignore[arg-type]

    def test_cropped_image_rejects_missing_bbox(self):
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=b"crop",
                bbox=None,  # type: ignore[arg-type]
                original_width=100,
                original_height=100,
                crop_width=10,
                crop_height=10,
                cropped=True,
            )

    def test_cropped_image_rejects_zero_area_bbox(self):
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=b"crop",
                bbox=[2, 2, 2, 10],
                original_width=100,
                original_height=100,
                crop_width=1,
                crop_height=8,
                cropped=True,
            )

    def test_cropped_image_rejects_full_image_bbox_geometry(self):
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=b"crop",
                bbox=[0, 0, 1000, 1000],
                original_width=1000,
                original_height=1000,
                crop_width=1000,
                crop_height=1000,
                cropped=True,
            )

    def test_cropped_image_rejects_near_full_area_ratio(self):
        with self.assertRaises(RuntimeError):
            CroppedImage(
                bytes=b"crop",
                bbox=[0, 0, 975, 975],
                original_width=1000,
                original_height=1000,
                crop_width=975,
                crop_height=975,
                cropped=True,
            )

    def test_cropped_image_accepts_reasonable_subset_crop(self):
        accepted = CroppedImage(
            bytes=b"crop",
            bbox=[100, 100, 600, 500],
            original_width=1000,
            original_height=1000,
            crop_width=500,
            crop_height=400,
            cropped=True,
        )
        self.assertEqual(accepted.crop_width, 500)


if __name__ == "__main__":
    unittest.main()
