"""Visualization helpers for debugging the QA pipeline.

When a banner gets flagged, the first question is "why?" — was the bbox
wrong, was the skeleton noisy, was the reference rendered at the wrong
font/size? These helpers render side-by-side comparisons so the answer
is visible at a glance.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .compare import CompareResult
from .detect import BBox


def draw_bboxes(
    image: Image.Image,
    bboxes: list[BBox],
    *,
    color: tuple[int, int, int] = (255, 0, 0),
    width: int = 3,
    labels: list[str] | None = None,
) -> Image.Image:
    """Return a copy of `image` with bbox rectangles drawn on top."""
    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    for i, b in enumerate(bboxes):
        draw.rectangle([b.x, b.y, b.x2, b.y2], outline=color, width=width)
        if labels and i < len(labels):
            draw.text((b.x + 4, b.y + 4), labels[i], fill=color)
    return out


def overlay_skeleton(
    image: Image.Image,
    skeleton: np.ndarray,
    *,
    color: tuple[int, int, int] = (255, 32, 32),
    dilate: int = 1,
) -> Image.Image:
    """Overlay a binary skeleton on top of `image` as coloured pixels."""
    base = image.convert("RGB").copy()
    h_base, w_base = np.array(base).shape[:2]
    h_sk, w_sk = skeleton.shape
    if (h_sk, w_sk) != (h_base, w_base):
        # Resize skeleton mask to match the base image dimensions.
        sk_img = Image.fromarray((skeleton * 255).astype(np.uint8)).resize(
            (w_base, h_base), Image.Resampling.NEAREST
        )
        mask = np.array(sk_img) > 127
    else:
        mask = skeleton > 0

    if dilate > 0:
        # Cheap dilation via Pillow MaxFilter — avoids importing cv2 just for
        # this one call.
        from PIL import ImageFilter
        mask_img = Image.fromarray((mask * 255).astype(np.uint8))
        mask_img = mask_img.filter(ImageFilter.MaxFilter(2 * dilate + 1))
        mask = np.array(mask_img) > 127

    arr = np.array(base)
    arr[mask] = color
    return Image.fromarray(arr)


def comparison_grid(
    banner_crop: Image.Image,
    ref_render: Image.Image,
    banner_sk: np.ndarray,
    ref_sk: np.ndarray,
    *,
    result: CompareResult | None = None,
    title: str | None = None,
) -> Image.Image:
    """Build a 2x2 visual grid for a flagged-block diagnosis.

    Top row:    banner crop          | reference render
    Bottom row: banner with skeleton | reference with skeleton

    `result` (CompareResult) is rendered as a footer.
    """
    cells = [
        ("banner", banner_crop.convert("RGB")),
        ("reference", ref_render.convert("RGB")),
        ("banner+skel", overlay_skeleton(banner_crop, banner_sk)),
        ("ref+skel", overlay_skeleton(ref_render, ref_sk)),
    ]

    # Resize all cells to the same dimensions for a tidy grid.
    cell_w = max(c[1].width for c in cells)
    cell_h = max(c[1].height for c in cells)
    cells_resized = [(label, _fit_into(img, cell_w, cell_h)) for label, img in cells]

    margin = 8
    label_h = 18
    footer_h = 40 if result is not None else 0
    title_h = 24 if title else 0

    grid_w = 2 * cell_w + 3 * margin
    grid_h = title_h + 2 * (cell_h + label_h) + 3 * margin + footer_h
    grid = Image.new("RGB", (grid_w, grid_h), (250, 250, 250))
    draw = ImageDraw.Draw(grid)

    y = margin
    if title:
        draw.text((margin, y), title, fill=(20, 20, 20))
        y += title_h

    for row in (0, 1):
        x = margin
        for col in (0, 1):
            label, img = cells_resized[row * 2 + col]
            draw.text((x, y), label, fill=(60, 60, 60))
            grid.paste(img, (x, y + label_h))
            x += cell_w + margin
        y += cell_h + label_h + margin

    if result is not None:
        tag = "FLAG" if result.flagged else "OK"
        footer = (
            f"[{tag}] score={result.score:.3f}  iou={result.iou:.3f}  "
            f"chamfer={result.chamfer:.2f}px  col={result.column_sim:.3f}"
        )
        draw.text((margin, y), footer, fill=(180, 0, 0) if result.flagged else (0, 120, 0))

    return grid


def _fit_into(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale an image to fit into (w, h) preserving aspect, pad with white."""
    if img.size == (w, h):
        return img
    src = img.copy()
    src.thumbnail((w, h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    x = (w - src.width) // 2
    y = (h - src.height) // 2
    canvas.paste(src, (x, y))
    return canvas


def save_grid(grid: Image.Image, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    grid.save(path)
    return path
