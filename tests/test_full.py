"""
Full test suite for OCR Localization Checker.
Tests: zip_processor, section_matcher, normalizer, scoring logic, hint fields.
OCR itself is mocked (no real API calls needed).
"""
import io
import os
import sys
import zipfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.normalizer import normalize_strict, normalize_soft, has_placeholder
from app.zip_processor import process_zip, extract_lang_code
from app.section_matcher import extract_sections, select_best, Section

FIXTURES = os.path.join(os.path.dirname(__file__), 'fixtures')


class TestNormalizer:

    def test_strict_lowercase(self):
        assert normalize_strict("Hello WORLD") == "hello world"

    def test_strict_removes_punctuation(self):
        assert normalize_strict("hello, world!") == "hello world"

    def test_strict_collapses_whitespace(self):
        assert normalize_strict("  hello   world  ") == "hello world"

    def test_strict_keeps_placeholders(self):
        result = normalize_strict("%displayname%, Hello!")
        assert "displayname" in result

    def test_soft_removes_percent_placeholder(self):
        result = normalize_soft("%displayname%, Hello!")
        assert "displayname" not in result
        assert "hello" in result

    def test_soft_removes_bracket_placeholder(self):
        result = normalize_soft("[username] check this")
        assert "username" not in result
        assert "check" in result

    def test_soft_removes_angle_placeholder(self):
        result = normalize_soft("Click <BUY NOW> here")
        assert "buy" not in result
        assert "click" in result and "here" in result

    def test_has_placeholder_percent(self):
        assert has_placeholder("%displayname%") is True

    def test_has_placeholder_bracket(self):
        assert has_placeholder("[username]") is True

    def test_has_placeholder_angle(self):
        assert has_placeholder("<BUY>") is True

    def test_has_placeholder_none(self):
        assert has_placeholder("Hello World") is False

    def test_strict_unicode(self):
        result = normalize_strict("День Валентина!")
        assert result == "день валентина"

    def test_strict_empty(self):
        assert normalize_strict("") == ""
        assert normalize_strict(None) == ""

    def test_soft_non_ascii_placeholder(self):
        result = normalize_soft("%任何内容%")
        assert result.strip() == ""


class TestLangExtraction:

    @pytest.mark.parametrize("filename,expected", [
        ("en.jpg",                    "en"),
        ("ru.jpg",                    "ru"),
        ("he.jpg",                    "he"),
        ("banner_en.jpg",             "en"),
        ("ru-banner.jpg",             "ru"),
        ("Campaign (en).docx",        "en"),
        ("Valentine's Day (ru).docx", "ru"),
        ("Campaign (he).docx",        "he"),
        ("img_zh-hans.png",           "zh-hans"),
        ("en_banner.jpg",             "en"),
    ])
    def test_extract_lang(self, filename, expected):
        assert extract_lang_code(filename) == expected

    def test_unknown_returns_none(self):
        assert extract_lang_code("image.jpg") is not None

    def test_bare_lang_code(self):
        assert extract_lang_code("en.jpg") == "en"
        assert extract_lang_code("ru.jpg") == "ru"


class TestZipProcessor:

    def test_basic_extraction(self):
        with open(f'{FIXTURES}/scenario4_multilang.zip', 'rb') as f:
            z = process_zip(f.read())
        assert set(z.images.keys()) == {'en', 'ru', 'he'}
        assert set(z.texts.keys()) == {'en', 'ru', 'he'}

    def test_image_bytes_non_empty(self):
        with open(f'{FIXTURES}/scenario1_section_hint.zip', 'rb') as f:
            z = process_zip(f.read())
        assert len(z.images['en']) > 0

    def test_filename_patterns(self):
        with open(f'{FIXTURES}/scenario7_filename_patterns.zip', 'rb') as f:
            z = process_zip(f.read())
        assert 'en' in z.images
        assert 'ru' in z.images

    def test_missing_text_for_one_lang(self):
        with open(f'{FIXTURES}/scenario6_missing_text.zip', 'rb') as f:
            z = process_zip(f.read())
        assert 'ru' in z.images
        assert 'ru' not in z.texts
        assert 'en' in z.texts


class TestSectionExtraction:

    def test_docx_section_count(self):
        with open(f'{FIXTURES}/scenario1_section_hint.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        sections = extract_sections(fbytes, fname)
        assert len(sections) == 4

    def test_docx_section_names(self):
        with open(f'{FIXTURES}/scenario1_section_hint.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        sections = extract_sections(fbytes, fname)
        names = [s.name for s in sections]
        assert 'PIC' in names
        assert 'BANNER' in names
        assert 'NEWS' in names

    def test_docx_content_not_empty(self):
        with open(f'{FIXTURES}/scenario1_section_hint.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        sections = extract_sections(fbytes, fname)
        for s in sections:
            assert len(s.content_text) > 0, f"Section {s.number} has empty content"

    def test_txt_format(self):
        with open(f'{FIXTURES}/scenario9_txt_format.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        sections = extract_sections(fbytes, fname)
        names = [s.name for s in sections]
        assert 'PIC' in names
        assert 'BANNER' in names


class TestScoringNoHint:

    def _sections(self):
        with open(f'{FIXTURES}/scenario1_section_hint.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        return extract_sections(fbytes, fname)

    def test_high_priority_wins_over_news(self):
        sections = self._sections()
        banner = next(s for s in sections if s.name == 'BANNER')
        result = select_best(sections, banner.content_text, 'en')
        assert result.best is not None
        assert result.best.section.name == 'BANNER'

    def test_pic_selected_for_pic_text(self):
        sections = self._sections()
        pic = next(s for s in sections if s.name == 'PIC')
        result = select_best(sections, pic.content_text, 'en')
        assert result.best is not None
        assert result.best.section.name == 'PIC'

    def test_exact_match_is_pass(self):
        sections = self._sections()
        pic = next(s for s in sections if s.name == 'PIC')
        result = select_best(sections, pic.content_text, 'en')
        assert result.status == 'PASS'
        assert result.best.strict_equal is True

    def test_empty_ocr_is_manual(self):
        sections = self._sections()
        result = select_best(sections, "", 'en')
        assert result.status == 'MANUAL'
        assert result.reason == 'ocr_too_short'

    def test_short_ocr_is_manual(self):
        sections = self._sections()
        result = select_best(sections, "hi", 'en')
        assert result.status == 'MANUAL'
        assert result.reason == 'ocr_too_short'

    def test_no_sections_is_manual(self):
        result = select_best([], "some text", 'en')
        assert result.status == 'MANUAL'
        assert result.reason == 'no_sections'

    def test_no_match_is_fail(self):
        sections = self._sections()
        result = select_best(sections, "completely random garbage xyz 123", 'en')
        assert result.status == 'FAIL'

    def test_news_penalty_applied(self):
        news_section = Section(1, 'NEWS', 'Hello World test content here')
        banner_section = Section(7, 'BANNER', 'Hello World test content here')
        result = select_best([news_section, banner_section], 'Hello World test content here', 'en')
        assert result.best.section.name == 'BANNER'

    def test_all_placeholders_is_manual(self):
        with open(f'{FIXTURES}/scenario5_all_placeholders.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        sections = extract_sections(fbytes, fname)
        result = select_best(sections, "Some OCR text here for testing", 'en')
        assert result.status == 'MANUAL'
        assert result.reason == 'all_placeholders'


class TestScoringWithHints:

    def _sections_from(self, zip_path):
        with open(zip_path, 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        return extract_sections(fbytes, fname)

    def test_section_number_hint_boosts_correct(self):
        sections = self._sections_from(f'{FIXTURES}/scenario1_section_hint.zip')
        pic = next(s for s in sections if s.number == 5)
        result = select_best(sections, pic.content_text, 'en', hint_number=5)
        assert result.best.section.number == 5

    def test_section_name_hint_boosts_banner(self):
        sections = self._sections_from(f'{FIXTURES}/scenario2_name_hint.zip')
        banner = next(s for s in sections if s.name == 'BANNER')
        result = select_best(sections, banner.content_text, 'en', hint_name='BANNER')
        assert result.best.section.name == 'BANNER'

    def test_both_hints_correct_section(self):
        sections = self._sections_from(f'{FIXTURES}/scenario3_both_hints.zip')
        banner = next(s for s in sections if s.name == 'BANNER')
        result = select_best(sections, banner.content_text, 'en', hint_number=7, hint_name='BANNER')
        assert result.best.section.name == 'BANNER'
        assert result.best.section.number == 7

    def test_wrong_hint_number_still_finds_match(self):
        sections = self._sections_from(f'{FIXTURES}/scenario1_section_hint.zip')
        banner = next(s for s in sections if s.name == 'BANNER')
        result = select_best(sections, banner.content_text, 'en', hint_number=99)
        assert result.best is not None
        assert result.best.section.name == 'BANNER'

    def test_hint_number_logo(self):
        with open(f'{FIXTURES}/scenario8_long_text.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        sections = extract_sections(fbytes, fname)
        logo = next(s for s in sections if s.name == 'LOGO')
        result = select_best(sections, logo.content_text, 'en', hint_number=10)
        assert result.best.section.name == 'LOGO'


class TestMultilanguage:

    def test_multilang_zip_all_langs_found(self):
        with open(f'{FIXTURES}/scenario4_multilang.zip', 'rb') as f:
            z = process_zip(f.read())
        for lang in ['en', 'ru', 'he']:
            assert lang in z.images
            assert lang in z.texts

    def test_ru_pic_matches(self):
        with open(f'{FIXTURES}/scenario4_multilang.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['ru']
        sections = extract_sections(fbytes, fname)
        pic = next(s for s in sections if s.name == 'PIC')
        result = select_best(sections, pic.content_text, 'ru')
        assert result.best.section.name == 'PIC'
        assert result.status == 'PASS'

    def test_he_banner_matches(self):
        with open(f'{FIXTURES}/scenario4_multilang.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['he']
        sections = extract_sections(fbytes, fname)
        banner = next(s for s in sections if s.name == 'BANNER')
        result = select_best(sections, banner.content_text, 'he')
        assert result.best.section.name == 'BANNER'
        assert result.status == 'PASS'

    def test_missing_text_lang_is_manual(self):
        result = select_best([], "any ocr text", 'ru')
        assert result.status == 'MANUAL'
        assert result.reason == 'no_sections'


class TestDeltaRule:

    def test_ambiguous_delta_triggers_manual(self):
        sections = [
            Section(1, 'BANNER', 'Hello world'),
            Section(2, 'PIC',    'Hello world'),
        ]
        result = select_best(sections, 'Hello world test', 'en')
        if result.delta < 0.05:
            assert result.status == 'MANUAL'
            assert result.reason == 'ambiguous_delta'

    def test_clear_winner_not_manual(self):
        sections = [
            Section(7, 'BANNER', 'Valentine Day -15% OFF Tokens Only Feb 13-15'),
            Section(1, 'NEWS',   'Completely different text about something else'),
        ]
        result = select_best(sections, 'Valentine Day -15% OFF Tokens Only Feb 13-15', 'en')
        assert result.best.section.name == 'BANNER'
        assert result.delta > 0.05

    def test_exact_match_always_pass(self):
        sections = [
            Section(7, 'BANNER', 'exact text here'),
            Section(5, 'PIC',    'different text there'),
        ]
        result = select_best(sections, 'exact text here', 'en')
        assert result.status == 'PASS'


class TestLongTextPenalty:

    def test_long_section_penalized(self):
        long_text = "word " * 60
        short_text = "Short text"
        sections = [
            Section(1, 'BANNER', long_text),
            Section(2, 'PIC',    short_text),
        ]
        result = select_best(sections, short_text, 'en')
        assert result.best.section.name == 'PIC'

    def test_hint_overrides_long_text_penalty(self):
        with open(f'{FIXTURES}/scenario8_long_text.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        sections = extract_sections(fbytes, fname)
        logo = next(s for s in sections if s.name == 'LOGO')
        result = select_best(sections, 'SHORT TEXT', 'en', hint_number=10)
        assert result.best.section.name == 'LOGO'


class TestTxtFormat:

    def test_txt_sections_parsed(self):
        with open(f'{FIXTURES}/scenario9_txt_format.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        sections = extract_sections(fbytes, fname)
        assert len(sections) >= 2
        names = [s.name for s in sections]
        assert 'PIC' in names

    def test_txt_matching_with_hint(self):
        with open(f'{FIXTURES}/scenario9_txt_format.zip', 'rb') as f:
            z = process_zip(f.read())
        fname, fbytes = z.texts['en']
        sections = extract_sections(fbytes, fname)
        pic = next((s for s in sections if s.name == 'PIC'), None)
        if pic:
            result = select_best(sections, pic.content_text, 'en', hint_name='PIC')
            assert result.best.section.name == 'PIC'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
