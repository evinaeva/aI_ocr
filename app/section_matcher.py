"""
Section matcher: parse DOCX/TXT, score candidates, pick best match.
"""
import io
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .normalizer import normalize_strict, normalize_soft, has_placeholder

# ─── Keyword scoring ────────────────────────────────────────────────────────
_HIGH_PRIORITY  = {"banner", "pic", "im", "popup"}
_PENALTY_WORDS  = {"news", "email", "letter", "subject"}

# Regex to detect section header like "5. PIC" at start of text
_SECTION_HEADER = re.compile(r"^\s*(\d+)\.\s+(\S+(?:\s+\S+){0,4}?)\s*$", re.IGNORECASE | re.MULTILINE)

# Languages that use characters instead of spaces
_CJK_LANGS = {"ja", "zh", "zh-hans", "zh-hant", "zh-cn", "zh-tw"}


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


# ─── DOCX parsing ───────────────────────────────────────────────────────────

def _cell_to_section(cell_text: str) -> Optional[Section]:
    """
    Parse a single DOCX cell like:
        "5. PIC\n\nActual content here"
    Returns Section or None.
    """
    cell_text = cell_text.strip()
    if not cell_text:
        return None

    lines = cell_text.splitlines()
    # Find header line: first non-empty line matching "N. NAME"
    header_line = ""
    content_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if m:
            header_line = stripped
            num = int(m.group(1))
            # Name is everything after "N. " up to end of first line
            name_raw = m.group(2).strip()
            content_start = i + 1
            # Content = remaining lines
            content_lines = [l for l in lines[content_start:]]
            content = "\n".join(content_lines).strip()
            return Section(
                number=num,
                name=name_raw,
                content_text=content,
                raw_header=header_line,
            )
        else:
            # No header found on first non-empty line; treat whole cell as content
            return Section(
                number=None,
                name="UNKNOWN",
                content_text=cell_text,
                raw_header="",
            )
    return None


def _text_from_docx_bytes(docx_bytes: bytes) -> List["Section"]:
    """Parse DOCX and return a list of Section objects directly."""
    from docx import Document  # type: ignore

    doc = Document(io.BytesIO(docx_bytes))
    sections = []

    if doc.tables:
        # Table-based: each row's first cell is a section
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    sec = _cell_to_section(cell.text)
                    if sec:
                        sections.append(sec)
    else:
        # Paragraph fallback — try Heading 2 style first, then line-based
        sections = _parse_sections_from_paragraphs(doc.paragraphs)
        if not sections or (len(sections) == 1 and sections[0].name == "UNKNOWN"):
            lines = [p.text for p in doc.paragraphs]
            sections = _parse_sections_from_lines(lines)

    return sections


def _text_from_txt_bytes(txt_bytes: bytes) -> List["Section"]:
    text = txt_bytes.decode("utf-8", errors="replace")
    return _parse_sections_from_lines(text.splitlines())


# ─── Heading 2 style section segmentation ────────────────────────────────────

def _parse_sections_from_paragraphs(paragraphs) -> List["Section"]:
    """
    Parse DOCX paragraphs using Heading 2 style as section delimiters.
    Falls back gracefully if no Heading 2 found.
    """
    sections: List["Section"] = []
    current_lines: List[str] = []
    current_name = "UNKNOWN"
    current_num: Optional[int] = None
    current_header = ""
    found_heading = False

    def flush():
        content = "\n".join(current_lines).strip()
        if content or current_header:
            sections.append(Section(
                number=current_num,
                name=current_name,
                content_text=content,
                raw_header=current_header,
            ))

    for para in paragraphs:
        style_name = para.style.name if para.style else ""
        text = para.text.strip()
        if not text:
            continue

        is_heading = "Heading 2" in style_name or "heading 2" in style_name.lower()

        # Also detect "N. NAME" pattern as heading regardless of style
        m = re.match(r"^(\d+)\.\s+(.+)$", text) if not is_heading else None

        if is_heading or m:
            found_heading = True
            if current_lines or current_header:
                flush()
                current_lines = []
            if m:
                current_num = int(m.group(1))
                current_name = m.group(2).strip()
                current_header = text
            else:
                # Heading 2 style, parse number if present
                m2 = re.match(r"^(\d+)\.\s+(.+)$", text)
                if m2:
                    current_num = int(m2.group(1))
                    current_name = m2.group(2).strip()
                else:
                    current_num = None
                    current_name = text
                current_header = text
        else:
            current_lines.append(text)

    if current_lines or current_header:
        flush()

    if not found_heading:
        return []  # Signal to caller to fall back to line-based

    return sections


# ─── Line-based section segmentation (fallback) ─────────────────────────────

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
                number=current_num,
                name=current_name,
                content_text=content,
                raw_header=current_header,
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
        m = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if m:
            if current_lines or current_header:
                flush()
                current_lines = []
            current_num = int(m.group(1))
            current_name = m.group(2).strip()
            current_header = stripped
        else:
            current_lines.append(stripped)

    if current_lines or current_header:
        flush()

    return sections


# ─── Public entry ────────────────────────────────────────────────────────────

def extract_sections(file_bytes: bytes, filename: str) -> List[Section]:
    fname_lower = filename.lower()
    if fname_lower.endswith(".docx"):
        return _text_from_docx_bytes(file_bytes)
    else:
        return _text_from_txt_bytes(file_bytes)


# ─── Scoring ─────────────────────────────────────────────────────────────────

def _count_tokens(text: str, lang: str) -> int:
    if lang in _CJK_LANGS:
        return len(re.sub(r"\s+", "", text))
    return len(text.split())


def _score_section(
    section: Section,
    ocr_text: str,
    lang: str,
) -> ScoredCandidate:
    warnings: List[str] = []
    score = 0.0

    # Use only the name keyword (first word before spaces)
    name_lower = section.name.split()[0].lower() if section.name else ""
    content = section.content_text

    # Base keyword score
    if name_lower in _HIGH_PRIORITY:
        score += 1.0
    elif name_lower in _PENALTY_WORDS:
        score -= 0.5

    # Placeholder penalty
    placeholder_flag = has_placeholder(content)
    if placeholder_flag:
        score *= 0.5
        warnings.append("has_placeholder")

    # Length penalty (non-CJK only)
    ocr_soft  = normalize_soft(ocr_text)
    cand_soft = normalize_soft(content)
    cand_tokens = _count_tokens(cand_soft, lang)

    if lang not in _CJK_LANGS and cand_tokens > 50:
        score -= 0.3
        warnings.append("long_text")

    # Strict equality
    ocr_strict  = normalize_strict(ocr_text)
    cand_strict = normalize_strict(content)
    strict_equal = bool(ocr_strict and cand_strict and ocr_strict == cand_strict)

    # Soft equality
    soft_equal = bool(ocr_soft and cand_soft and ocr_soft == cand_soft)

    # Big boost for actual match
    if strict_equal:
        score += 5.0
    elif soft_equal:
        score += 3.0
    else:
        # Partial similarity (token overlap / Jaccard)
        ocr_tokens_set  = set(ocr_soft.split())
        cand_tokens_set = set(cand_soft.split())
        if ocr_tokens_set and cand_tokens_set:
            overlap = len(ocr_tokens_set & cand_tokens_set)
            union   = len(ocr_tokens_set | cand_tokens_set)
            jaccard = overlap / union if union else 0
            score += jaccard * 2.0

    return ScoredCandidate(
        section=section,
        score=score,
        strict_equal=strict_equal,
        soft_equal=soft_equal,
        has_placeholder_flag=placeholder_flag,
        warnings=warnings,
    )


# ─── Selection ───────────────────────────────────────────────────────────────

def select_best(
    sections: List[Section],
    ocr_text: str,
    lang: str,
    hint_number: Optional[int] = None,
    hint_name: Optional[str] = None,
) -> SelectionResult:
    if not sections:
        return SelectionResult(
            best=None, all_candidates=[], manual_required=True,
            status="MANUAL", delta=0.0, reason="no_sections"
        )

    ocr_stripped = ocr_text.strip()
    if len(ocr_stripped) < 3:
        return SelectionResult(
            best=None, all_candidates=[], manual_required=True,
            status="MANUAL", delta=0.0, reason="ocr_too_short"
        )

    # ── HINT HARD FILTER (Bug 2 fix) ─────────────────────────────────────────
    # Apply hint as hard constraint BEFORE scoring, not as a bonus.
    # Only filter if at least one section passes — otherwise fall through to all.
    filtered = sections
    if hint_number is not None:
        by_num = [s for s in sections if s.number == hint_number]
        if by_num:
            filtered = by_num
    if hint_name:
        by_name = [s for s in filtered if hint_name.lower() in s.name.lower()]
        if by_name:
            filtered = by_name
    # If filtering left us with sections, use them; otherwise fall back to all
    working_sections = filtered if filtered != sections or (hint_number is None and not hint_name) else filtered

    candidates = [
        _score_section(s, ocr_text, lang)
        for s in working_sections
    ]
    candidates.sort(key=lambda c: c.score, reverse=True)

    top1 = candidates[0]
    top2 = candidates[1] if len(candidates) > 1 else None
    delta = (top1.score - top2.score) if top2 else 999.0

    # All candidates have placeholders → MANUAL
    if all(c.has_placeholder_flag for c in candidates):
        return SelectionResult(
            best=top1, all_candidates=candidates, manual_required=True,
            status="MANUAL", delta=delta, reason="all_placeholders"
        )

    # Delta too small and no strict equal → MANUAL
    if delta < 0.05 and not top1.strict_equal:
        return SelectionResult(
            best=top1, all_candidates=candidates, manual_required=True,
            status="MANUAL", delta=delta, reason="ambiguous_delta"
        )

    # Determine status
    if top1.strict_equal:
        status = "PASS"
        manual = False
        reason = "strict_equal"
    elif top1.soft_equal:
        status = "MANUAL"
        manual = True
        reason = "soft_equal_only"
    else:
        status = "FAIL"
        manual = False
        reason = "no_match"

    return SelectionResult(
        best=top1,
        all_candidates=candidates,
        manual_required=manual,
        status=status,
        delta=delta,
        reason=reason,
    )
