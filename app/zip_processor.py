"""
ZIP processor: extract images and text files, map by language code.
"""
import io
import re
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Patterns to extract language code from filename (stem only, no extension)
# Tried in order; first match wins.
_LANG_PATTERNS = [
    re.compile(r"\(([a-zA-Z]{2,5}(?:-[a-zA-Z]{2,8})?)\)"),          # (en), (zh-Hans)
    re.compile(r"[-_ .]([a-zA-Z]{2,5}(?:-[a-zA-Z]{2,8})?)$"),        # _en, -en, .en at end of stem
    re.compile(r"^([a-zA-Z]{2,5}(?:-[a-zA-Z]{2,8})?)[-_ .]"),        # en_banner, en-banner
    re.compile(r"^([a-zA-Z]{2,5}(?:-[a-zA-Z]{2,8})?)$"),              # bare: "en", "ru", "he"
]

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
_TEXT_EXT  = {".docx", ".txt"}


def extract_lang_code(filename: str) -> Optional[str]:
    """Extract language code from a filename (basename only)."""
    name = filename.rsplit("/", 1)[-1]  # basename
    stem = name.rsplit(".", 1)[0] if "." in name else name
    for pattern in _LANG_PATTERNS:
        m = pattern.search(stem)
        if m:
            return m.group(1).lower()
    return None


@dataclass
class ZipContents:
    # lang_code -> raw bytes of image
    images: Dict[str, bytes] = field(default_factory=dict)
    # lang_code -> (original_filename, raw bytes)
    texts: Dict[str, tuple] = field(default_factory=dict)
    # lang_code -> original image filename (for display)
    image_names: Dict[str, str] = field(default_factory=dict)
    # lang_code -> original text filename
    text_names: Dict[str, str] = field(default_factory=dict)


def process_zip(zip_bytes: bytes) -> ZipContents:
    """
    Read a ZIP archive from bytes.
    Returns ZipContents mapping language codes to images and text files.
    """
    contents = ZipContents()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.endswith("/"):
                continue  # directory entry

            lower = name.lower()
            _, ext = (name.rsplit(".", 1) if "." in name else (name, ""))
            ext = "." + ext.lower() if ext else ""

            if "images/" in lower and ext in _IMAGE_EXT:
                lang = extract_lang_code(name)
                if lang:
                    contents.images[lang] = zf.read(info)
                    contents.image_names[lang] = name

            elif "texts/" in lower and ext in _TEXT_EXT:
                lang = extract_lang_code(name)
                if lang:
                    contents.texts[lang] = (name, zf.read(info))
                    contents.text_names[lang] = name

    return contents
