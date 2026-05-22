"""
French typography rules — preserved through normalisation when lang='fr'.

Per Imprimerie nationale rules, a space is mandatory:
  - BEFORE: `!`, `?`, `:`, `;`, `»`, `%`
  - AFTER: `«`

So `MONDE !` (correct French) and `MONDE!` (English-style) must NOT
normalise to the same value when lang='fr' — otherwise QA would silently
pass banners that violate French typography.

Operator feedback (2026-05): "у французов перед некоторыми знаками
препинания должен стоять пробел по правилам. Пробел во французском не
должен схлопываться."
"""
import pytest

from app.normalizer import normalize, clean_for_display


# ── Space before `!`, `?`, `:`, `;` for lang='fr' ────────────────────────────

@pytest.mark.parametrize(
    "punct",
    ["!", "?", ":", ";"],
)
def test_fr_space_before_punct_is_preserved(punct):
    space_form = f"MONDE {punct}"
    nospace_form = f"MONDE{punct}"
    assert normalize(space_form, "strict", lang="fr") != normalize(nospace_form, "strict", lang="fr")
    # Specifically, the space form keeps the space token in normalisation
    assert " " + punct in normalize(space_form, "strict", lang="fr")


def test_fr_strict_norm_du_monde_distinct():
    """The actual screenshot regression: `DU MONDE !` vs `DU MONDE!`."""
    a = normalize("DU MONDE !", "strict", lang="fr")
    b = normalize("DU MONDE!", "strict", lang="fr")
    assert a != b


def test_non_fr_lang_default_strict_keeps_existing_behaviour():
    """For non-FR locales the strict normalisation hasn't changed —
    `!` still survives (it's in the default `_STRIP_STRICT_RE` keep set)
    so `DU MONDE !` and `DU MONDE!` were always distinct."""
    assert normalize("DU MONDE !", "strict", lang="en") != normalize("DU MONDE!", "strict", lang="en")


# ── Guillemets `« »` preserved for fr only ───────────────────────────────────

def test_fr_guillemets_preserved_through_pre_clean():
    """Default behaviour folds `« »` to `"`. For fr we keep them so the
    LLM judge sees the typography character it needs to evaluate."""
    out_fr = clean_for_display("« Bonjour »", lang="fr")
    out_en = clean_for_display("« Bonjour »", lang="en")
    assert "«" in out_fr and "»" in out_fr
    assert "«" not in out_en and "»" not in out_en
    # Folded variant should contain straight quotes
    assert '"' in out_en


def test_fr_space_after_opening_guillemet():
    """`«Bonjour»` (no inner space) vs `« Bonjour »` (with) — different in fr."""
    a = normalize("«Bonjour»", "strict", lang="fr")
    b = normalize("« Bonjour »", "strict", lang="fr")
    assert a != b


# ── Narrow no-break space (U+202F) folds to ASCII space ──────────────────────

def test_narrow_nbsp_folds_to_space():
    """U+202F (narrow no-break space) is what French typography uses
    before `! ? : ; »`. OCR engines may preserve it or convert to ASCII;
    we fold to ASCII so both versions compare equal at the SPACE level
    (but still distinct from no-space)."""
    nnbsp = "Monde !"
    ascii_space = "Monde !"
    assert normalize(nnbsp, "strict", lang="fr") == normalize(ascii_space, "strict", lang="fr")


def test_regular_nbsp_folds_to_space():
    """U+00A0 (no-break space) was already folded — re-affirm under fr."""
    nbsp = "Monde !"
    ascii_space = "Monde !"
    assert normalize(nbsp, "strict", lang="fr") == normalize(ascii_space, "strict", lang="fr")


# ── Other languages unchanged ────────────────────────────────────────────────

@pytest.mark.parametrize("lang", ["en", "ru", "de", "es", "ja", None])
def test_other_langs_strip_guillemets(lang):
    """Only lang='fr' preserves `« »`. Everything else folds to `"`."""
    out = clean_for_display("« Hello »", lang=lang)
    assert "«" not in out
    assert "»" not in out
    assert '"' in out


def test_fr_colon_typography_distinct():
    """`Voir : ici` (correct French) vs `Voir: ici` — different in fr."""
    assert normalize("Voir : ici", "strict", lang="fr") != normalize("Voir: ici", "strict", lang="fr")


def test_non_fr_colon_collapses():
    """For non-fr the default keep-set drops `:`, so both variants collapse."""
    assert normalize("Voir : ici", "strict", lang="en") == normalize("Voir: ici", "strict", lang="en")
