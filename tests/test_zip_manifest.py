import io
import unittest
import zipfile

from app.zip_processor import build_zip_manifest


PNG_MIN = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
    b'\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
    b'\x00\x00\x00\x0bIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfeA\r\x1f\xb7'
    b'\x00\x00\x00\x00IEND\xaeB`\x82'
)


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestZipManifest(unittest.TestCase):
    def test_grouped_structure_creates_multiple_target_ids(self):
        z = _zip({
            "700/en.png": PNG_MIN,
            "700/ru.png": PNG_MIN,
            "1080/en.png": PNG_MIN,
        })
        manifest = build_zip_manifest(z)
        self.assertEqual([m.target_id for m in manifest], ["1080", "700"])

    def test_flat_structure_creates_single_target_id(self):
        z = _zip({
            "banner_en_700x420.png": PNG_MIN,
            "banner_ru_700x420.png": PNG_MIN,
        })
        manifest = build_zip_manifest(z)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0].target_id, "default")


    def test_non_target_folder_name_does_not_become_target_id(self):
        z = _zip({
            "images/en.png": PNG_MIN,
            "images/ru.png": PNG_MIN,
        })
        manifest = build_zip_manifest(z)
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0].target_id, "default")

    def test_missing_en_is_detectable_per_target(self):
        z = _zip({
            "700/ru.png": PNG_MIN,
            "700/de.png": PNG_MIN,
            "1080/en.png": PNG_MIN,
        })
        targets = {m.target_id: m for m in build_zip_manifest(z)}
        self.assertFalse(targets["700"].has_en)
        self.assertTrue(targets["1080"].has_en)

    def test_target_bboxes_are_propagated_to_manifest_items(self):
        z = _zip({
            "700/en.png": PNG_MIN,
            "1080/en.png": PNG_MIN,
        })
        targets = {m.target_id: m for m in build_zip_manifest(z, target_bboxes={"700": [1, 2, 30, 40]})}
        self.assertEqual(targets["700"].items[0].bbox, [1, 2, 30, 40])
        self.assertIsNone(targets["1080"].items[0].bbox)


if __name__ == "__main__":
    unittest.main()
