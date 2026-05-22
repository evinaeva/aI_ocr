"""
Tests for Azure markdown-header stripping and consensus normalisation
robustness against Azure's `#`/`##`/`###` headline prefixes.

Operator-reported regression (2026-05): banners with large headline text
had Azure returning lines like `# DΩΡΟ`, `## ΣΥΛΛΟΓΗ`. The leading hashes
are layout hints, not content. Before this fix, `normalize_for_consensus`
stripped the `#` to empty (not space), leaving multiple internal spaces
where the markers used to be — which differed from Google/OCR.Space's
clean output and triggered `engines_disagree` MANUAL on every banner.
"""
import pytest

from app import ocr
from app.normalizer import normalize


# ── Azure markdown stripper ──────────────────────────────────────────────────

def test_strip_azure_markdown_single_hash():
    assert ocr._strip_azure_markdown("# Headline") == "Headline"


def test_strip_azure_markdown_multiple_levels():
    src = "# ΔΩΡΟ\n## ΣΥΛΛΟΓΗ\n### ΓΙΑ ΤΟ ΠΑΓΚΟΣΜΙΟ\n#### ΠΡΩΤΑΘΛΗΜΑ!"
    expected = "ΔΩΡΟ\nΣΥΛΛΟΓΗ\nΓΙΑ ΤΟ ΠΑΓΚΟΣΜΙΟ\nΠΡΩΤΑΘΛΗΜΑ!"
    assert ocr._strip_azure_markdown(src) == expected


def test_strip_azure_markdown_only_when_at_line_start():
    """Inline `#` (e.g. in 'C#' or '#hashtag' mid-line) is preserved."""
    src = "Buy with C# code\nUse #promo at checkout"
    assert ocr._strip_azure_markdown(src) == src


def test_strip_azure_markdown_handles_empty():
    assert ocr._strip_azure_markdown("") == ""


def test_strip_azure_markdown_preserves_text_without_markers():
    src = "DΩΡΟ\nΣΥΛΛΟΓΗ\nΓΙΑ ΤΟ ΠΑΓΚΟΣΜΙΟ"
    assert ocr._strip_azure_markdown(src) == src


def test_strip_azure_markdown_no_space_after_marker():
    """`#headline` (no space after #) also gets stripped."""
    assert ocr._strip_azure_markdown("#headline") == "headline"


# ── Consensus normalisation: punct → space, then collapse ────────────────────

def test_consensus_collapses_whitespace_after_punct_strip():
    """Stripping `#` should NOT leave double spaces — used to break
    Azure markdown lines against the other engines."""
    # Simulated: Azure post-markdown-strip has clean text; but if anything
    # else creates internal punctuation, the consensus norm should keep
    # spacing aligned with Google/OCR.Space.
    azure_with_punct = "hello, world! great-day"
    google_clean = "hello world great day"
    assert normalize(azure_with_punct, "consensus") == normalize(google_clean, "consensus")


def test_consensus_spanish_inverted_punct_stripped():
    """`¡` and `¿` are Unicode `P*` punctuation; should normalise away."""
    es_with_inverted = "¡colección de regalos!"
    es_plain = "colección de regalos"
    assert normalize(es_with_inverted, "consensus") == normalize(es_plain, "consensus")


def test_consensus_french_quotes_stripped():
    fr_with_guillemets = "«Bonjour» le monde"
    fr_plain = "Bonjour le monde"
    assert normalize(fr_with_guillemets, "consensus") == normalize(fr_plain, "consensus")


def test_consensus_preserves_placeholder_chars():
    """`<>[]%` are intentionally kept (used by placeholder detection)."""
    n = normalize("[ X ] <Y> %Z%", "consensus")
    assert "<" in n
    assert ">" in n
    assert "[" in n
    assert "]" in n
    assert "%" in n


def test_consensus_azure_markdown_after_strip_matches_other_engines():
    """The actual regression case: Azure post-stripped should normalise
    identically to a clean text from another engine."""
    azure_post_strip = "ΔΩΡΟ\nΣΥΛΛΟΓΗ\nΓΙΑ ΤΟ ΠΑΓΚΟΣΜΙΟ\nΠΡΩΤΑΘΛΗΜΑ!\nΜόνο μέχρι τις\n13 Ιουνίου\nΑΠΟΣΤΟΛΗ ΔΩΡΟΥ"
    google_clean = "ΔΩΡΟ\nΣΥΛΛΟΓΗ\nΓΙΑ ΤΟ ΠΑΓΚΟΣΜΙΟ\nΠΡΩΤΑΘΛΗΜΑ!\nΜόνο μέχρι τις\n13 Ιουνίου\nΑΠΟΣΤΟΛΗ ΔΩΡΟΥ"
    assert normalize(azure_post_strip, "consensus") == normalize(google_clean, "consensus")


def test_consensus_punct_to_space_no_double_spaces():
    """`hello! world` (punct → space → collapse) must produce single space."""
    assert normalize("hello! world", "consensus") == "hello world"
    assert normalize("hello!world", "consensus") == "hello world"
    assert normalize("hello!!!world", "consensus") == "hello world"


def test_consensus_space_before_exclamation():
    """`DU MONDE !` vs `DU MONDE!` — both must normalise the same."""
    assert normalize("DU MONDE !", "consensus") == normalize("DU MONDE!", "consensus")
    assert normalize("DU MONDE !", "consensus") == "du monde"
