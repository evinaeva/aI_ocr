"""
Section matcher: parse DOCX/TXT, score candidates, pick best match.
"""
import io
import re
from dataclasses import dataclass, field
from typing import List, Optional

from .normalizer import normalize_strict, normalize_soft, has_placeholder

# ─── Keyword scoring ──────────────────────────────────────────────────────────────
HIGH_PRIORITY_NAMES = {"banner", "pic", "im", "popup"}
PENALTY_NAMES = {"news", "email", "letter", "subject"}

# Languages that use characters instead of spaces (no word tokenisation)
_CJK_LANGS = {"ja", "zh", "zh-hans", "zh-hant", "zh-cn", "zh-tw"}

# Section header regex: "5. NAME"
# Handles various unicode separators seen in localized DOCX files:
#   .       U+002E  standard full stop
#   ․       U+2024  ONE DOT LEADER (used in Armenian/hy rows 1-3)
#   ։       U+0589  ARMENIAN FULL STOP
#   ۔       U+06D4  ARABIC FULL STOP (Urdu)
#   ．       U+FF0E  FULLWIDTH FULL STOP (Japanese DOCX)
#   no sep         e.g. "5 ՆԿԱՌ" (number followed directly by space+name)
_HEADER_RE = re.compile(
    r"^(\d+)"                           # leading digit(s)
    r"[.\u2024\u0589\u06D4\uFF0E]?"    # optional separator
    r"\s*"                               # zero or more spaces
    r"([^\d].*)$",                       # section name (must not start with digit)
    re.IGNORECASE,
)

# Characters to strip from the beginning of a parsed section name
# (e.g. fullwidth dot ． left after regex group capture)
_NAME_LEADING_STRIP_RE = re.compile(r"^[\s.\u2024\u0589\u06D4\uFF0E]+")


@dataclass
class Section:
    number: Optional[int]
    name: str
    content_text: str
    raw_header: str = ""


@dataclass
class ScoredCandidate:
    section: Section
    score: float
    strict_equal: bool
    soft_equal: bool
    has_placeholder_flag: bool
    warnings: List[str] = field(default_factory=list)


@dataclass
class SelectionResult:
    best: Optional[ScoredCandidate]
    all_candidates: List[ScoredCandidate]
    manual_required: bool
    status: str          # "PASS" | "FAIL" | "MANUAL"
    delta: float         # top1.score - top2.score
    reason: str


# ─── DOCX parsing ───────────────────────────────────────────────────────────────

def _parse_header(line: str) -> Optional[tuple]:
    """
    Try to parse a section header. Returns (number, name) or None.
    Strips leading punctuation artifacts from the name (e.g. ． from Japanese DOCX).
    """
    stripped = line.strip()
    m = _HEADER_RE.match(stripped)
    if m:
        name = _NAME_LEADING_STRIP_RE.sub("", m.group(2)).strip()
        return int(m.group(1)), name
    return None


def _cell_to_section(cell_text: str) -> Optional[Section]:
    """Parse a single DOCX table cell into a Section."""
    cell_text = cell_text.strip()
    if not cell_text:
        return None

    lines = cell_text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        parsed = _parse_header(line)
        if parsed:
            num, name = parsed
            content = "\n".join(lines[i + 1:]).strip()
            return Section(number=num, name=name,
                           content_text=content, raw_header=line.strip())
        else:
            return Section(number=None, name="UNKNOWN",
                           content_text=cell_text, raw_header="")
    return None


def _text_from_docx_bytes(docx_bytes: bytes) -> List[Section]:
    from docx import Document  # type: ignore
    doc = Document(io.BytesIO(docx_bytes))
    sections = []

    if doc.tables:
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    sec = _cell_to_section(cell.text)
                    if sec:
                        sections.append(sec)
    else:
        sections = _parse_sections_from_paragraphs(doc.paragraphs)
        if not sections or (len(sections) == 1 and sections[0].name == "UNKNOWN"):
            lines = [p.text for p in doc.paragraphs]
            sections = _parse_sections_from_lines(lines)

    return sections


def _text_from_txt_bytes(txt_bytes: bytes) -> List[Section]:
    text = txt_bytes.decode("utf-8", errors="replace")
    return _parse_sections_from_lines(text.splitlines())


# ─── Heading 2 style segmentation ────────────────────────────────────────────────

def _parse_sections_from_paragraphs(paragraphs) -> List[Section]:
    sections: List[Section] = []
    current_lines: List[str] = []
    current_name = "UNKNOWN"
    current_num: Optional[int] = None
    current_header = ""
    found_heading = False

    def flush():
        content = "\n".join(current_lines).strip()
        if content or current_header:
            sections.append(Section(
                number=current_num, name=current_name,
                content_text=content, raw_header=current_header,
            ))

    for para in paragraphs:
        style_name = para.style.name if para.style else ""
        text = para.text.strip()
        if not text:
            continue

        is_heading = "Heading 2" in style_name or "heading 2" in style_name.lower()
        parsed = _parse_header(text) if not is_heading else None

        if is_heading or parsed:
            found_heading = True
            if current_lines or current_header:
                flush()
                current_lines = []
            if parsed:
                current_num, current_name = parsed
                current_header = text
            else:
                p2 = _parse_header(text)
                if p2:
                    current_num, current_name = p2
                else:
                    current_num = None
                    current_name = text
                current_header = text
        else:
            current_lines.append(text)

    if current_lines or current_header:
        flush()

    return sections if found_heading else []


# ─── Line-based segmentation (fallback) ─────────────────────────────────────────

def _parse_sections_from_lines(lines: List[str]) -> List[Section]:
    sections: List[Section] = []
    current_lines: List[str] = []
    current_num: Optional[int] = None
    current_name = "UNKNOWN"
    current_header = ""
    blank_run = 0

    def flush():
        content = "\n".join(current_lines).strip()
        if content or current_header:
            sections.append(Section(
                number=current_num, name=current_name,
                content_text=content, raw_header=current_header,
            ))

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            blank_run += 1
            if blank_run >= 2 and current_lines:
                flush()
                current_lines = []
                current_header = ""
                current_num = None
                current_name = "UNKNOWN"
            else:
                current_lines.append("")
            continue

        blank_run = 0
        parsed = _parse_header(stripped)
        if parsed:
            if current_lines or current_header:
                flush()
                current_lines = []
            current_num, current_name = parsed
            current_header = stripped
        else:
            current_lines.append(stripped)

    if current_lines or current_header:
        flush()

    return sections


# ─── Public entry ─────────────────────────────────────────────────────────────────

def extract_sections(file_bytes: bytes, filename: str) -> List[Section]:
    fname_lower = filename.lower()
    if fname_lower.endswith(".docx"):
        return _text_from_docx_bytes(file_bytes)
    else:
        return _text_from_txt_bytes(file_bytes)


# ─── Scoring ─────────────────────────────────────────────────────────────────────

def _count_tokens(text: str, lang: str) -> int:
    if lang in _CJK_LANGS:
        return len(re.sub(r"\s+", "", text))
    return len(text.split())


def _name_score(name: str) -> float:
    """Return priority score based on section name.
    Uses only the first ASCII-normalized word for matching.
    """
    # Strip leading non-word chars, take first word, ASCII-normalize
    clean = re.sub(r"^[^\w]+", "", name, flags=re.UNICODE)
    first = clean.split()[0].lower() if clean.split() else ""
    # Normalize full-width letters to ASCII (e.g. PIC -> pic)
    first = first.encode('ascii', errors='ignore').decode('ascii')
    if first in HIGH_PRIORITY_NAMES:
        return 1.0
    if first in PENALTY_NAMES:
        return -0.5
    return 0.0


def _score_section(section: Section, ocr_text: str, lang: str) -> ScoredCandidate:
    warnings: List[str] = []
    score = _name_score(section.name)

    content = section.content_text
    placeholder_flag = has_placeholder(content)
    if placeholder_flag:
        score *= 0.5
        warnings.append("has_placeholder")

    ocr_soft  = normalize_soft(ocr_text)
    cand_soft = normalize_soft(content)
    cand_tokens = _count_tokens(cand_soft, lang)

    if lang not in _CJK_LANGS and cand_tokens > 50:
        score -= 0.3
        warnings.append("long_text")

    ocr_strict  = normalize_strict(ocr_text)
    cand_strict = normalize_strict(content)
    strict_equal = bool(ocr_strict and cand_strict and ocr_strict == cand_strict)
    soft_equal   = bool(ocr_soft and cand_soft and ocr_soft == cand_soft)

    if strict_equal:
        score += 5.0
    elif soft_equal:
        score += 3.0
    else:
        ocr_tokens_set  = set(ocr_soft.split())
        cand_tokens_set = set(cand_soft.split())
        if ocr_tokens_set and cand_tokens_set:
            overlap = len(ocr_tokens_set & cand_tokens_set)
            union   = len(ocr_tokens_set | cand_tokens_set)
            score  += (overlap / union) * 2.0 if union else 0

    return ScoredCandidate(
        section=section, score=score,
        strict_equal=strict_equal, soft_equal=soft_equal,
        has_placeholder_flag=placeholder_flag, warnings=warnings,
    )


# ─── Selection ────────────────────────────────────────────────────────────────────

def select_best(
    sections: List[Section],
    ocr_text: str,
    lang: str,
    hint_number: Optional[int] = None,
    hint_name: Optional[str] = None,
) -> SelectionResult:
    if not sections:
        return SelectionResult(best=None, all_candidates=[], manual_required=True,
                               status="MANUAL", delta=0.0, reason="no_sections")

    if len(ocr_text.strip()) < 3:
        return SelectionResult(best=None, all_candidates=[], manual_required=True,
                               status="MANUAL", delta=0.0, reason="ocr_too_short")

    # ── HINT HARD FILTER ──────────────────────────────────────────────────────────
    filtered = sections
    if hint_number is not None:
        by_num = [s for s in sections if s.number == hint_number]
        if by_num:
            filtered = by_num
    if hint_name and len(filtered) > 1:
        by_name = [s for s in filtered if hint_name.lower() in s.name.lower()]
        if by_name:
            filtered = by_name

    candidates = [_score_section(s, ocr_text, lang) for s in filtered]
    candidates.sort(key=lambda c: c.score, reverse=True)

    top1  = candidates[0]
    top2  = candidates[1] if len(candidates) > 1 else None
    delta = (top1.score - top2.score) if top2 else 999.0

    # all_placeholders: only trigger if hint narrowed to a single section
    # OR if all filtered candidates have placeholders
    if all(c.has_placeholder_flag for c in candidates):
        # If there's a hint (number or name) and we matched exactly one section,
        # still try to match — don't bail out as all_placeholders
        if hint_number is not None and len(filtered) == 1:
            pass  # fall through to normal scoring
        else:
            return SelectionResult(best=top1, all_candidates=candidates, manual_required=True,
                                   status="MANUAL", delta=delta, reason="all_placeholders")

    if delta < 0.05 and not top1.strict_equal:
        return SelectionResult(best=top1, all_candidates=candidates, manual_required=True,
                               status="MANUAL", delta=delta, reason="ambiguous_delta")

    if top1.strict_equal:
        return SelectionResult(best=top1, all_candidates=candidates, manual_required=False,
                               status="PASS", delta=delta, reason="strict_equal")
    if top1.soft_equal:
        return SelectionResult(best=top1, all_candidates=candidates, manual_required=True,
                               status="MANUAL", delta=delta, reason="soft_equal_only")

    return SelectionResult(best=top1, all_candidates=candidates, manual_required=False,
                           status="FAIL", delta=delta, reason="no_match")
