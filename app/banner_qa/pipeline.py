"""End-to-end QA pipeline for one banner.

Heavy lifting (font matching, robust skeleton extraction from real banners) lives
in next iteration. This module wires the existing pieces together so the smoke
test exercises the full path: detect -> render reference -> skeletonize both ->
compare -> report.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from .compare import CompareResult, compare_skeletons
from .detect import BBox, detect_blocks
from .fonts import FontSpec, render_text_in_bbox
from .report import BannerReport, BlockReport
from .skeleton import extract_skeleton


def qa_block(
    banner_crop: Image.Image,
    reference_text: str,
    font: FontSpec,
    *,
    threshold: float = 0.30,
) -> tuple[CompareResult, Image.Image]:
    """Compare one block crop against a reference rendered in `font`.

    Reference is rendered into the SAME (W, H) as the banner crop so the
    canvas-normalisation step in compare_skeletons has no aspect-ratio
    drift to absorb.
    """
    w, h = banner_crop.size
    banner_sk = extract_skeleton(banner_crop)
    ref_img = render_text_in_bbox(reference_text, font, w, h)
    ref_sk = extract_skeleton(ref_img)
    result = compare_skeletons(banner_sk, ref_sk, threshold=threshold)
    return result, ref_img


def qa_banner(
    banner_path: str | Path,
    reference_text: str,
    font: FontSpec,
    *,
    language: str = "unknown",
    threshold: float = 0.30,
) -> BannerReport:
    """Single-block / single-language QA. Real multi-block matching comes later."""
    img = Image.open(banner_path).convert("RGBA")
    blocks: list[BBox] = detect_blocks(banner_path)
    report = BannerReport(banner_path=str(banner_path), language=language)
    for bbox in blocks:
        crop = bbox.crop(img)
        result, _ref_img = qa_block(crop, reference_text, font, threshold=threshold)
        report.blocks.append(BlockReport(bbox=bbox, reference_text=reference_text, compare=result))
    return report
