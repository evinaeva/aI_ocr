"""Font catalog and rendering.

V1 catalog covers 4 families. Some weights are delivered as separate files
(.ttf/.otf), some as a single variable font with named weight instances
(Open Sans). `FontSpec.variation` selects the instance at render time.

User-delivered files live in `fonts/` at the repo root.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = REPO_ROOT / "fonts"


@dataclass(frozen=True)
class FontSpec:
    family: str
    weight: str
    filename: str  # basename in fonts/
    variation: str | None = None  # for variable fonts: name of weight instance

    @property
    def path(self) -> Path:
        return FONTS_DIR / self.filename

    def exists(self) -> bool:
        return self.path.is_file()


# Canonical catalog. Reflects exactly the weights the user has delivered.
# Missing: Bebas Neue Bold (spec asked for it; only Regular was provided).
CATALOG: list[FontSpec] = [
    # Open Sans — variable font, one file, 5 named weight instances.
    FontSpec("Open Sans", "Light", "OpenSans.ttf", variation="Light"),
    FontSpec("Open Sans", "Regular", "OpenSans.ttf", variation="Regular"),
    FontSpec("Open Sans", "SemiBold", "OpenSans.ttf", variation="SemiBold"),
    FontSpec("Open Sans", "Bold", "OpenSans.ttf", variation="Bold"),
    FontSpec("Open Sans", "ExtraBold", "OpenSans.ttf", variation="ExtraBold"),
    # Helvetica Neue — .otf, separate files per weight.
    FontSpec("Helvetica Neue", "Thin", "HelveticaNeue-Thin.otf"),
    FontSpec("Helvetica Neue", "Light", "HelveticaNeue-Light.otf"),
    FontSpec("Helvetica Neue", "Roman", "HelveticaNeue-Roman.otf"),
    FontSpec("Helvetica Neue", "Medium", "HelveticaNeue-Medium.otf"),
    FontSpec("Helvetica Neue", "Bold", "HelveticaNeue-Bold.otf"),
    # Bebas Neue — Regular only.
    FontSpec("Bebas Neue", "Regular", "BebasNeue-Regular.ttf"),
    # Gilroy — non-italic weights.
    FontSpec("Gilroy", "UltraLight", "Gilroy-UltraLight.ttf"),
    FontSpec("Gilroy", "Thin", "Gilroy-Thin.ttf"),
    FontSpec("Gilroy", "Light", "Gilroy-Light.ttf"),
    FontSpec("Gilroy", "Regular", "Gilroy-Regular.ttf"),
    FontSpec("Gilroy", "Medium", "Gilroy-Medium.ttf"),
    FontSpec("Gilroy", "Semibold", "Gilroy-Semibold.ttf"),
    FontSpec("Gilroy", "Bold", "Gilroy-Bold.ttf"),
    FontSpec("Gilroy", "Extrabold", "Gilroy-Extrabold.ttf"),
    FontSpec("Gilroy", "Heavy", "Gilroy-Heavy.ttf"),
    FontSpec("Gilroy", "Black", "Gilroy-Black.ttf"),
]


def available_fonts() -> list[FontSpec]:
    return [f for f in CATALOG if f.exists()]


def find(family: str, weight: str) -> FontSpec | None:
    for f in CATALOG:
        if f.family.lower() == family.lower() and f.weight.lower() == weight.lower():
            return f
    return None


def render_text(
    text: str,
    font: FontSpec | str | Path,
    pixel_height: int,
    padding: int = 4,
) -> Image.Image:
    """Render `text` in the given font at the target visual pixel height.

    `font` accepts a FontSpec (preferred — variation is honored for variable
    fonts) or a raw file path. Returns RGBA with black glyphs on a
    transparent background, tightly cropped with a small padding.
    """
    path, variation = _resolve_font(font)
    pil_font = _font_at_height(path, pixel_height, variation)

    dummy = Image.new("L", (10, 10))
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), text, font=pil_font)
    w = bbox[2] - bbox[0] + 2 * padding
    h = bbox[3] - bbox[1] + 2 * padding

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((padding - bbox[0], padding - bbox[1]), text, fill=(0, 0, 0, 255), font=pil_font)
    return img


def render_text_in_bbox(
    text: str,
    font: FontSpec | str | Path,
    bbox_w: int,
    bbox_h: int,
    padding: int = 4,
) -> Image.Image:
    """Render `text` at the largest size that fits inside (bbox_w, bbox_h).

    Returns an RGBA image exactly bbox_w × bbox_h. Text is centred; black
    on transparent background. Used in the QA pipeline to render the
    reference into the same dimensions as a CRAFT-detected banner block.
    """
    path, variation = _resolve_font(font)
    max_w = max(1, bbox_w - 2 * padding)
    max_h = max(1, bbox_h - 2 * padding)
    pil_font = _font_to_fit_bbox(path, text, max_w, max_h, variation)

    dummy = Image.new("L", (10, 10))
    draw = ImageDraw.Draw(dummy)
    tbbox = draw.textbbox((0, 0), text, font=pil_font)
    text_w = tbbox[2] - tbbox[0]
    text_h = tbbox[3] - tbbox[1]

    img = Image.new("RGBA", (bbox_w, bbox_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    x = (bbox_w - text_w) // 2 - tbbox[0]
    y = (bbox_h - text_h) // 2 - tbbox[1]
    draw.text((x, y), text, fill=(0, 0, 0, 255), font=pil_font)
    return img


def _font_to_fit_bbox(
    font_path: str,
    text: str,
    max_w: int,
    max_h: int,
    variation: str | None = None,
) -> ImageFont.FreeTypeFont:
    """Binary-search the largest font size where `text` fits in (max_w, max_h)."""
    lo, hi = 4, max_h * 3
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(font_path, mid, variation)
        dummy = Image.new("L", (10, 10))
        draw = ImageDraw.Draw(dummy)
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= max_w and h <= max_h:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return _load_font(font_path, best, variation)


def _resolve_font(font: FontSpec | str | Path) -> tuple[str, str | None]:
    if isinstance(font, FontSpec):
        return str(font.path), font.variation
    return str(font), None


def _font_at_height(
    font_path: str,
    target_h: int,
    variation: str | None = None,
) -> ImageFont.FreeTypeFont:
    """Binary-search the font size that yields glyphs ≈ target_h pixels tall.

    Pillow's `size=` is roughly the em size; visible cap-height is smaller and
    font-dependent, so we measure the rendered height of a reference string.
    """
    lo, hi = 4, target_h * 3
    best = lo
    ref = "Hg"
    while lo <= hi:
        mid = (lo + hi) // 2
        font = _load_font(font_path, mid, variation)
        dummy = Image.new("L", (10, 10))
        draw = ImageDraw.Draw(dummy)
        bbox = draw.textbbox((0, 0), ref, font=font)
        h = bbox[3] - bbox[1]
        if h <= target_h:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return _load_font(font_path, best, variation)


def _load_font(path: str, size: int, variation: str | None) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size)
    if variation:
        try:
            font.set_variation_by_name(variation)
        except (OSError, AttributeError):
            # Not a variable font, or this Pillow doesn't support it — fall
            # back to the default weight.
            pass
    return font
