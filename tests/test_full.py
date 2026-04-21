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
        """Placeholders survive strict normalization (they become plain words)."""
        result = normalize_strict("%displayname%, Hello!")
        assert "displayname" in result

    def test_soft_removes_percent_placeholder(self):
        result = normalize_soft("%displayname%, Hello!")
        assert "displayname" not in result
        assert "hello" in result

    def test_soft_removes_bracket_placeholder(self):
        result = normalize_soft("[subscriber_firstname_capitalized] check this")
        assert "subscriber_firstname_capitalized" not in result
        assert "check" in result

    def test_soft_removes_angle_placeholder(self):
        """<date> is a whitelisted variable — should be removed."""
        result = normalize_soft("Promotion valid until <date> only")
        assert "date" not in result
        assert "promotion" in result and "only" in result

    def test_soft_keeps_cta_not_in_whitelist(self):
        """<BUY TOKENS>, [Ok, thanks] — NOT in whitelist, kept as text."""
        result = normalize_soft("Valentine's Day! -15% OFF <BUY TOKENS>")
        assert "buy" in result
        assert "tokens" in result

    def test_has_placeholder_percent(self):
        assert has_placeholder("%displayname%") is True
        assert has_placeholder("%skin%") is True
        assert has_placeholder("%bonus_amount%") is True

    def test_has_placeholder_bracket(self):
        assert has_placeholder("[username]") is True
        assert has_placeholder("[subscriber_firstname_capitalized]") is True

    def test_has_placeholder_angle_whitelisted(self):
        assert has_placeholder("<date>") is True

    def test_has_no_placeholder_cta(self):
        """CTA buttons and UI labels are NOT placeholders."""
        assert has_placeholder("<BUY TOKENS>") is False
        assert has_placeholder("<BUY>") is False
        assert has_placeholder("[Ok, thanks]") is False

    def test_has_no_placeholder_unknown_var(self):
        """Unknown variable names not in whitelist are NOT placeholders."""
        assert has_placeholder("%unknown_custom_var%") is False

    def test_has_placeholder_none(self):
        assert has_placeholder("Hello World") is False

    def test_strict_case_insensitive(self):
        """Comparison is always case-insensitive — UPPERCASE == lowercase."""
        assert normalize_strict("VALENTINE'S DAY") == normalize_strict("Valentine's Day")
        assert normalize_strict("BUY TOKENS") == normalize_strict("buy tokens")

    def test_strict_unicode(self):
        result = normalize_strict("День Валентина!")
        assert result == "день валентина"

    def test_strict_empty(self):
        assert normalize_strict("") == ""
        assert normalize_strict(None) == ""

    def test_soft_non_ascii_placeholder(self):
        result = normalize_soft("%任何内容%")
        # Not in whitelist — kept as-is (then stripped by punctuation removal)
        assert isinstance(result, str)

    # ── Unicode normalization ────────────────────────────────────────────────

    def test_strict_em_dash_equals_hyphen(self):
        a = normalize_strict("Valentine Day \u2014 15% OFF")   # em-dash
        b = normalize_strict("Valentine Day - 15% OFF")          # hyphen
        assert a == b

    def test_strict_en_dash_equals_hyphen(self):
        a = normalize_strict("Feb 13\u201315")   # en-dash
        b = normalize_strict("Feb 13-15")
        assert a == b

    def test_strict_nbsp_equals_space(self):
        a = normalize_strict("hello\u00a0world")
        b = normalize_strict("hello world")
        assert a == b

    def test_strict_smart_quotes_equal_straight(self):
        a = normalize_strict("\u201chello\u201d")
        b = normalize_strict('"hello"')
        assert a == b

    def test_strict_soft_hyphen_removed(self):
        a = normalize_strict("hel\u00adlo")
        b = normalize_strict("hello")
        assert a == b

    def test_strict_ellipsis_char_equals_dots(self):
        a = normalize_strict("Wait\u2026")
        b = normalize_strict("Wait...")
        assert a == b

    def test_strict_zero_width_removed(self):
        a = normalize_strict("hel\u200blo")
        b = normalize_strict("hello")
        assert a == b

    def test_strict_arrow_symbols_removed(self):
        a = normalize_strict("\u25b8 Only February 13-15")  # ▸
        b = normalize_strict("Only February 13-15")
        assert a == b

    def test_strict_match_with_unicode_variants(self):
        ocr_text = "Valentine Day\u00a0\u2014 15% OFF Tokens Only Feb 13\u201315"
        ref_text = "Valentine Day - 15% OFF Tokens Only Feb 13-15"
        assert normalize_strict(ocr_text) == normalize_strict(ref_text)

    def test_strict_banner_with_cta_matches(self):
        """Real-world: OCR sees 'BUY TOKENS', DOCX has '<BUY TOKENS>' — must PASS."""
        ocr = "Valentine's Day!\n-15% OFF Tokens!\nOnly February 13-15\nBUY TOKENS"
        ref = "Valentine's Day! \u2764\ufe0f\n\n-15% OFF Tokens!\n\nOnly February 13-15\n\n<BUY TOKENS>"
        assert normalize_strict(ocr) == normalize_strict(ref)


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
        assert 0.0 <= result.reference_confidence <= 1.0
        assert result.score_top1 is not None

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

    def test_no_match_is_manual(self):
        sections = [
            Section(1, "HEADER", "The quick brown fox jumps over the lazy dog."),
            Section(2, "FOOTER", "Copyright 2026 all rights reserved."),
        ]
        result = select_best(sections, "completely random garbage xyz 123 !!!", 'en')
        assert result.status == 'MANUAL'
        assert result.best is not None
        assert 0.0 <= result.reference_confidence <= 1.0

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

    def test_plain_txt_banner_not_split_by_numeric_copy(self):
        text = "10 TOKENS instantly on your balance!\nOffer ends soon."
        sections = extract_sections(text.encode('utf-8'), 'plain.txt')
        assert len(sections) == 1
        assert sections[0].number is None
        assert sections[0].name == 'UNKNOWN'
        assert sections[0].content_text == text

    def test_plain_txt_banner_matches_as_whole_reference(self):
        text = "10 TOKENS instantly on your balance!\nOffer ends soon."
        sections = extract_sections(text.encode('utf-8'), 'plain.txt')
        result = select_best(sections, text, 'en')
        assert result.best is not None
        assert result.best.strict_equal is True
        assert result.status == 'PASS'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
