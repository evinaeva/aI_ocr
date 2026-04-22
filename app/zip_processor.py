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
import logging
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ── Language whitelist and tokenization helpers ─────────────────────────────
_SUPPORTED_LANG_CODES = {
    'zn',
    'ar', 'az', 'bg', 'cs', 'da', 'de', 'el', 'en', 'es', 'et', 'fa', 'fi', 'fr',
    'mk',
    'he', 'hi', 'hr', 'hu', 'hy', 'id', 'it', 'ja', 'ka', 'kk', 'ko', 'lt', 'lv',
    'nl', 'no', 'pl', 'pt', 'ro', 'ru', 'sk', 'sl', 'sr', 'sv', 'th', 'tr', 'uk',
    'ur', 'vi', 'zh', 'cn', 'kr', 'il', 'in', 'gr', 'se', 'dk', 'ua', 'ee', 'kz',
    'rs', 'cz', 'jp', 'si',
}
_COMPOSITE_LANG_CODES = {'zh-hans', 'zh-hant', 'pt-pt', 'sr-latn', 'az-latn', 'kk-cyrl', 'mk-mk'}
_TOKEN_SPLIT_RE = re.compile(r"[^a-zA-Z]+")

# Tokens that look like lang codes but aren't
_NON_LANG = {
    "runetki", "logo", "icon", "btn", "mid", "bg", "bm", "bc", "wl",
    "bonga", "vday", "promo", "email", "feb", "day", "valentine",
    "img", "done", "transl", "banner", "preview", "default", "thumb",
    "brb", "bng", "bgm", "new", "top", "all", "web", "app",
}

# Image-specific shortened codes → standard codes (to match DOCX lang codes)
_LANG_NORMALIZE = {
    "cn":  "zh",
    "zh":  "zh",
    "zn":  "zh",
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
    "pt":  "pt",
    "az":  "az",
    "si":  "sl",
}

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
_TEXT_EXT  = {".docx", ".txt"}
_SIZE_RE   = re.compile(r"\d{2,4}x\d{2,4}")

# Known brand/variant prefixes that appear before the real lang code
_BRAND_PREFIXES = {"wl", "bonga", "bc", "bm", "vday"}
logger = logging.getLogger(__name__)


def _normalize_lang(code: str) -> str:
    c = code.lower()
    return _LANG_NORMALIZE.get(c, c)


def extract_lang_code(filename: str) -> Optional[str]:
    """Extract and normalize language code from an archive path or filename."""
    archive_path = filename
    name = filename.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem_lower = stem.lower()

    def _log_decision(raw_code: Optional[str], normalized: Optional[str], final_code: Optional[str]) -> Optional[str]:
        logger.debug(
            "lang_decision archive_path=%r filename=%r raw=%r normalized=%r final=%r",
            archive_path,
            name,
            raw_code,
            normalized,
            final_code,
        )
        return final_code

    if stem_lower in _NON_LANG and stem_lower not in _SUPPORTED_LANG_CODES:
        return _log_decision(None, None, None)

    if re.fullmatch(r"[a-zA-Z]{2}", stem):
        normalized = _normalize_lang(stem)
        return _log_decision(stem, normalized, normalized)

    stem_clean = _SIZE_RE.sub("", stem_lower).strip("_- .")
    stem_paren = stem.lower()

    # explicit composite codes first
    paren_match = re.search(r'\(([^)]+)\)', stem_paren)
    if paren_match:
        inner = paren_match.group(1).strip().lower()
        if inner in _COMPOSITE_LANG_CODES:
            normalized = _normalize_lang(inner)
            return _log_decision(inner, normalized, normalized)
        if inner in _SUPPORTED_LANG_CODES:
            normalized = _normalize_lang(inner)
            return _log_decision(inner, normalized, normalized)

    for code in sorted(_COMPOSITE_LANG_CODES, key=len, reverse=True):
        if re.search(rf'(?<![a-z]){re.escape(code)}(?![a-z])', stem_clean):
            normalized = _normalize_lang(code)
            return _log_decision(code, normalized, normalized)

    # then plain tokenized lookup, only from whitelist
    tokens = [t for t in _TOKEN_SPLIT_RE.split(stem_clean) if t]
    for i, tok in enumerate(tokens):
        if tok in _BRAND_PREFIXES and i + 1 < len(tokens):
            candidate = tokens[i + 1]
            if candidate in _SUPPORTED_LANG_CODES:
                normalized = _normalize_lang(candidate)
                return _log_decision(candidate, normalized, normalized)

    for tok in reversed(tokens):
        if tok in _SUPPORTED_LANG_CODES:
            normalized = _normalize_lang(tok)
            return _log_decision(tok, normalized, normalized)

    if tokens:
        last_two = '-'.join(tokens[-2:]) if len(tokens) >= 2 else None
        if last_two and last_two in _COMPOSITE_LANG_CODES:
            normalized = _normalize_lang(last_two)
            return _log_decision(last_two, normalized, normalized)

    return _log_decision(None, None, None)


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
    bbox: Optional[list[int]] = None
    zone_name: Optional[str] = None
    expected_by_lang: Dict[str, str] = field(default_factory=dict)


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
    if len(parts) > 1:
        return "/".join(parts[:-1])
    return "default"


def build_zip_manifest(
    zip_bytes: bytes,
    *,
    target_bboxes: Optional[Dict[str, list[int]]] = None,
    target_zones: Optional[Dict[str, list[dict[str, Any]]]] = None,
) -> list[ZipTargetManifest]:
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
        zones = []
        if target_zones and target_id in target_zones:
            zones = target_zones[target_id]
        elif target_bboxes:
            candidate = target_bboxes.get(target_id)
            if isinstance(candidate, list) and len(candidate) == 4:
                zones = [{"bbox": [int(v) for v in candidate], "zone_name": None, "expected_by_lang": {}}]

        if zones:
            for zone in zones:
                bbox = zone.get("bbox") if isinstance(zone, dict) else None
                if not isinstance(bbox, list) or len(bbox) != 4:
                    continue
                expected_by_lang = zone.get("expected_by_lang") if isinstance(zone, dict) else None
                item = ZipManifestItem(
                    archive_path=path,
                    lang=lang,
                    target_id=target_id,
                    bbox=[int(v) for v in bbox],
                    zone_name=(zone.get("zone_name") if isinstance(zone, dict) else None),
                    expected_by_lang=(expected_by_lang if isinstance(expected_by_lang, dict) else {}),
                )
                target.items.append(item)
        else:
            item = ZipManifestItem(archive_path=path, lang=lang, target_id=target_id, bbox=None)
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
