import unittest

from app.zip_processor import extract_lang_code


class TestExtractLangCode(unittest.TestCase):
    def test_two_letter_filename_lowercase(self):
        self.assertEqual(extract_lang_code("ru.png"), "ru")

    def test_two_letter_filename_uppercase(self):
        self.assertEqual(extract_lang_code("EN.JPG"), "en")

    def test_unknown_filename_falls_back_to_und(self):
        self.assertIsNone(extract_lang_code("banner.png"))

    def test_existing_mapping_is_unchanged(self):
        self.assertEqual(extract_lang_code("cn.jpg"), "zh-hans")


if __name__ == "__main__":
    unittest.main()
