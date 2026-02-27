"""
ZIP processor: supports both legacy format (images/ + texts/ folders)
and real-world format (outer ZIP with two inner ZIPs: one for images, one for texts).

Real archive structures seen in production:
  outer.zip
    ├── BNG-29724.zip     ← images (subfolders by size: 300/, 550/, 700/, 800/, 1080/)
    └── Campaign.zip      ← texts  (docx files, no subfolders)

Image filename patterns:
  PREVDAY2026MODELS_en_1080x1920.jpg  → {name}_{lang}_{WxH}
  BRB-en-700x420.jpg                  → {name}-{lang}-{WxH}
  VDAY2026_wl_en_350x320.jpg          → {name}_{brand}_{lang}_{WxH}  ← brand prefix
  en.png                              → bare {lang}.png
"""

import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, Optional

# ── Lang patterns (tried in order, first match wins) ─────────────────────────
_LANG_PATTERNS = [
    re.compile(r"\(([a-zA-Z]{2,10}(?:-[a-zA-Z]{2,10})?)\)"),         # (en), (zh-Hans), (pt-PT)
    re.compile(r"[-_ ]([a-zA-Z]{2,5}(?:-[a-zA-Z]{2,8})?)[-_ .]"),    # _en_, -en-
    re.compile(r"[-_ ]([a-zA-Z]{2,5}(?:-[a-zA-Z]{2,8})?)$"),          # _en, -en at end
    re.compile(r"^([a-zA-Z]{2,5}(?:-[a-zA-Z]{2,8})?)[-_ .]"),         # en_banner
    re.compile(r"^([a-zA-Z]{2,5}(?:-[a-zA-Z]{2,8})?)$"),              # bare "en"
]

# Tokens that look like lang codes but aren't
_NON_LANG = {
    "runetki", "logo", "icon", "btn", "mid", "bg", "bm", "bc", "wl",
    "bonga", "vday", "promo", "email", "feb", "day", "valentine",
    "img", "done", "transl", "banner", "preview", "default", "thumb",
    "brb", "bng", "bgm", "new", "top", "all", "web", "app",
}

# Image-specific shortened codes → standard codes (to match DOCX lang codes)
_LANG_NORMALIZE = {
    "cn":  "zh-hans",
    "zh":  "zh-hans",
    "kr":  "ko",
    "il":  "he",
    "in":  "hi",
    "gr":  "el",
    "se":  "sv",
    "dk":  "da",
    "ua":  "uk",
    "ee":  "et",
    "kz":  "kk",
    "rs":  "sr-latn",
    "sr":  "sr-latn",
    "cz":  "cs",
    "jp":  "ja",
    "pt":  "pt-pt",
    "az":  "az-latn",
    "si":  "sl",
}

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
_TEXT_EXT  = {".docx", ".txt"}
_SIZE_RE   = re.compile(r"\d{2,4}x\d{2,4}")

# Known brand/variant prefixes that appear before the real lang code
_BRAND_PREFIXES = {"wl", "bonga", "bc", "bm", "vday"}


def _normalize_lang(code: str) -> str:
    c = code.lower()
    return _LANG_NORMALIZE.get(c, c)


def extract_lang_code(filename: str) -> Optional[str]:
    """Extract and normalize language code from a filename (basename only)."""
    name = filename.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem_lower = stem.lower()

    # Skip known non-language filenames
    if stem_lower in _NON_LANG:
        return None

    # Strip size pattern before matching (e.g. _1080x1920)
    stem_clean = _SIZE_RE.sub("", stem).rstrip("_- ")

    # Special case: {PREFIX}_{BRAND}_{lang}_{size} e.g. VDAY2026_wl_ar_350x320
    tokens = re.split(r"[-_]", stem_clean.lower())
    for i, tok in enumerate(tokens):
        if tok in _BRAND_PREFIXES and i + 1 < len(tokens):
            candidate = tokens[i + 1]
            if candidate not in _NON_LANG and not re.match(r"^\d+$", candidate) and len(candidate) >= 2:
                return _normalize_lang(candidate)

    for pattern in _LANG_PATTERNS:
        m = pattern.search(stem_clean)
        if m:
            code = m.group(1).lower()
            if code in _NON_LANG:
                continue
            if re.match(r"^\d+$", code):
                continue
            return _normalize_lang(code)
    return None


def _image_priority(path: str) -> int:
    """Larger resolution = lower number = preferred (sort ascending)."""
    m = _SIZE_RE.search(path)
    if m:
        w, h = m.group().split("x")
        return -(int(w) * int(h))
    return 0


@dataclass
class ZipContents:
    images: Dict[str, bytes] = field(default_factory=dict)
    texts: Dict[str, tuple] = field(default_factory=dict)
    image_names: Dict[str, str] = field(default_factory=dict)
    text_names: Dict[str, str] = field(default_factory=dict)


@dataclass
class ZipManifestItem:
    archive_path: str
    lang: Optional[str]
    target_id: str


@dataclass
class ZipTargetManifest:
    target_id: str
    has_en: bool
    items: list[ZipManifestItem] = field(default_factory=list)


def _iter_image_archive_paths(zf: zipfile.ZipFile, *, prefix: str = "") -> list[str]:
    out: list[str] = []
    for info in zf.infolist():
        if info.filename.endswith("/"):
            continue
        lower = info.filename.lower()
        if lower.endswith(".zip"):
            try:
                nested = zipfile.ZipFile(io.BytesIO(zf.read(info)))
            except zipfile.BadZipFile:
                continue
            with nested:
                out.extend(_iter_image_archive_paths(nested, prefix=f"{prefix}{info.filename}!/"))
            continue

        ext = ("." + info.filename.rsplit(".", 1)[-1].lower()) if "." in info.filename else ""
        if ext in _IMAGE_EXT:
            out.append(f"{prefix}{info.filename}")
    return out


def _is_target_segment(segment: str) -> bool:
    s = (segment or "").strip().lower()
    return bool(re.match(r"^\d+$", s) or re.match(r"^\d+x\d+$", s))

def _infer_target_id(path: str, grouped: bool) -> str:
    clean = path.split("!/", 1)[-1]
    parts = [p for p in clean.split("/") if p]
    if grouped and len(parts) >= 2 and _is_target_segment(parts[0]):
        return parts[0]
    return "default"


def build_zip_manifest(zip_bytes: bytes) -> list[ZipTargetManifest]:
    """
    Build backend ZIP manifest grouped by target_id.

    Keeps per-file archive_path verbatim, extracted lang and deterministic target_id,
    and exposes EN presence per target.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        paths = _iter_image_archive_paths(zf)

    grouped = False
    for path in paths:
        clean = path.split("!/", 1)[-1]
        parts = [p for p in clean.split("/") if p]
        if len(parts) >= 2 and _is_target_segment(parts[0]):
            grouped = True
            break
    targets: dict[str, ZipTargetManifest] = {}

    for path in sorted(paths):
        basename = path.split("!/", 1)[-1].rsplit("/", 1)[-1]
        lang = extract_lang_code(basename)
        target_id = _infer_target_id(path, grouped)
        target = targets.setdefault(target_id, ZipTargetManifest(target_id=target_id, has_en=False))
        item = ZipManifestItem(archive_path=path, lang=lang, target_id=target_id)
        target.items.append(item)
        if lang == "en":
            target.has_en = True

    return [targets[k] for k in sorted(targets.keys())]


def _is_images_zip(zf: zipfile.ZipFile) -> bool:
    return any(
        not i.filename.endswith("/") and
        "." in i.filename and
        "." + i.filename.rsplit(".", 1)[-1].lower() in _IMAGE_EXT
        for i in zf.infolist()
    )


def _is_texts_zip(zf: zipfile.ZipFile) -> bool:
    return any(
        not i.filename.endswith("/") and
        "." in i.filename and
        "." + i.filename.rsplit(".", 1)[-1].lower() in _TEXT_EXT
        for i in zf.infolist()
    )


def _extract_images(zf: zipfile.ZipFile, contents: ZipContents) -> None:
    """Extract one image per language — pick largest resolution."""
    candidates: dict[str, list] = {}
    for info in zf.infolist():
        if info.filename.endswith("/"):
            continue
        basename = info.filename.rsplit("/", 1)[-1]
        ext = ("." + basename.rsplit(".", 1)[-1].lower()) if "." in basename else ""
        if ext not in _IMAGE_EXT:
            continue
        lang = extract_lang_code(basename)
        if not lang:
            continue
        priority = _image_priority(info.filename)
        candidates.setdefault(lang, []).append((priority, info.filename))

    for lang, options in candidates.items():
        options.sort(key=lambda x: x[0])
        best_path = options[0][1]
        contents.images[lang] = zf.read(best_path)
        contents.image_names[lang] = best_path


def _extract_texts(zf: zipfile.ZipFile, contents: ZipContents) -> None:
    """Extract one text file per language."""
    for info in zf.infolist():
        if info.filename.endswith("/"):
            continue
        basename = info.filename.rsplit("/", 1)[-1]
        ext = ("." + basename.rsplit(".", 1)[-1].lower()) if "." in basename else ""
        if ext not in _TEXT_EXT:
            continue
        lang = extract_lang_code(basename)
        if not lang:
            continue
        if lang not in contents.texts:
            contents.texts[lang] = (basename, zf.read(info))
            contents.text_names[lang] = basename


def _process_flat_zip(zf: zipfile.ZipFile, contents: ZipContents) -> None:
    """Legacy format: images/ and texts/ subfolders in one ZIP."""
    img_candidates: dict[str, list] = {}
    for info in zf.infolist():
        if info.filename.endswith("/"):
            continue
        lower = info.filename.lower()
        basename = info.filename.rsplit("/", 1)[-1]
        ext = ("." + basename.rsplit(".", 1)[-1].lower()) if "." in basename else ""

        if "images/" in lower and ext in _IMAGE_EXT:
            lang = extract_lang_code(basename)
            if lang:
                priority = _image_priority(info.filename)
                img_candidates.setdefault(lang, []).append((priority, info.filename))
        elif "texts/" in lower and ext in _TEXT_EXT:
            lang = extract_lang_code(basename)
            if lang and lang not in contents.texts:
                contents.texts[lang] = (basename, zf.read(info))
                contents.text_names[lang] = basename

    for lang, options in img_candidates.items():
        options.sort(key=lambda x: x[0])
        best_path = options[0][1]
        contents.images[lang] = zf.read(best_path)
        contents.image_names[lang] = best_path


def process_zip(zip_bytes: bytes) -> ZipContents:
    """
    Parse upload ZIP. Supports:
    1. Outer ZIP with two inner ZIPs (images-ZIP + texts-ZIP)  ← production format
    2. Legacy flat ZIP with images/ and texts/ subfolders
    3. Flat ZIP with mixed content
    """
    contents = ZipContents()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as outer:
        inner_zips = [
            i for i in outer.infolist()
            if not i.filename.endswith("/") and i.filename.lower().endswith(".zip")
        ]

        if inner_zips:
            # Production format: outer ZIP with inner ZIPs
            for inner_info in inner_zips:
                inner_bytes = outer.read(inner_info)
                try:
                    with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner_zf:
                        has_img = _is_images_zip(inner_zf)
                        has_txt = _is_texts_zip(inner_zf)
                        if has_txt and not has_img:
                            _extract_texts(inner_zf, contents)
                        elif has_img and not has_txt:
                            _extract_images(inner_zf, contents)
                        else:
                            _extract_images(inner_zf, contents)
                            _extract_texts(inner_zf, contents)
                except zipfile.BadZipFile:
                    pass
        else:
            # Legacy / flat format
            has_images_folder = any("images/" in i.filename.lower() for i in outer.infolist())
            has_texts_folder  = any("texts/"  in i.filename.lower() for i in outer.infolist())
            if has_images_folder or has_texts_folder:
                _process_flat_zip(outer, contents)
            else:
                _extract_images(outer, contents)
                _extract_texts(outer, contents)

    return contents
