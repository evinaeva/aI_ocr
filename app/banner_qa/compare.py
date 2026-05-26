"""Compare two skeleton masks and return a similarity score.

For Scenario A (zero missed typos) we want a metric that:
  - returns ~1.0 for genuinely identical text rendered with the same font
  - drops sharply for any character-level difference
  - tolerates 1–2 px alignment jitter and minor anti-aliasing remnants

Approach: resize both to a common canvas, dilate slightly (jitter tolerance),
compute IoU of skeleton pixels, then symmetric Chamfer distance for shape
sensitivity. Composite score = IoU * (1 - normalized chamfer).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from skimage.morphology import skeletonize as sk_skeletonize


@dataclass
class CompareResult:
    score: float          # 0..1, higher = more similar
    iou: float
    chamfer: float        # mean min-distance, in pixels
    column_sim: float     # 0..1, sensitive to letter-order changes
    flagged: bool         # True if below threshold

    def __str__(self) -> str:
        tag = "FLAG" if self.flagged else "OK  "
        return (
            f"[{tag}] score={self.score:.3f} iou={self.iou:.3f} "
            f"chamfer={self.chamfer:.2f}px col={self.column_sim:.3f}"
        )


def compare_skeletons(
    sk_a: np.ndarray,
    sk_b: np.ndarray,
    *,
    canvas: tuple[int, int] = (128, 512),  # (h, w)
    dilate_px: int = 2,
    threshold: float = 0.30,
) -> CompareResult:
    """Compare two skeleton masks.

    Combines three signals:
      - IoU after small dilation (broad shape match)
      - symmetric Chamfer distance (point-set distance, penalizes drift)
      - column-projection L1 distance (sensitive to letter-order changes —
        catches transposition typos that IoU alone misses)

    `threshold` is for Scenario A — tune on real data once available.
    """
    a = _fit_to_canvas(sk_a, canvas)
    b = _fit_to_canvas(sk_b, canvas)

    a_d = _dilate(a, dilate_px)
    b_d = _dilate(b, dilate_px)

    inter = (a_d & b_d).sum()
    union = (a_d | b_d).sum()
    iou = float(inter) / float(union) if union > 0 else 0.0

    chamfer = _symmetric_chamfer(a, b)
    chamfer_penalty = min(chamfer / 10.0, 1.0)  # 10px ~ "very different"

    column_sim = _column_projection_similarity(a, b)

    score = iou * (1.0 - chamfer_penalty) * column_sim

    return CompareResult(
        score=score,
        iou=iou,
        chamfer=chamfer,
        column_sim=column_sim,
        flagged=score < threshold,
    )


def _fit_to_canvas(sk: np.ndarray, canvas: tuple[int, int]) -> np.ndarray:
    """Scale skeleton to fit canvas while preserving aspect, then center-pad."""
    h_c, w_c = canvas
    if sk.size == 0 or sk.sum() == 0:
        return np.zeros(canvas, dtype=np.uint8)

    # Crop to bounding box of skeleton pixels.
    ys, xs = np.where(sk > 0)
    y0, y1 = ys.min(), ys.max() + 1
    x0, x1 = xs.min(), xs.max() + 1
    crop = sk[y0:y1, x0:x1]

    h, w = crop.shape
    scale = min(h_c / h, w_c / w)
    new_h = max(1, int(round(h * scale)))
    new_w = max(1, int(round(w * scale)))

    # Two-stage resize to avoid 1-px rasterisation noise from INTER_NEAREST:
    #   1) Dilate the source skeleton to a 3-px brush so it survives smooth
    #      downscaling.
    #   2) cv2.INTER_AREA produces a smooth grayscale ramp; threshold back.
    #   3) Re-skeletonise on the canvas so we end up with clean 1-px lines
    #      that depend on shape, not on the input grid alignment.
    thick = cv2.dilate(
        crop.astype(np.uint8) * 255,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )
    resized = cv2.resize(thick, (new_w, new_h), interpolation=cv2.INTER_AREA)
    binary = (resized > 96).astype(bool)
    if binary.any():
        clean = sk_skeletonize(binary).astype(np.uint8)
    else:
        clean = binary.astype(np.uint8)

    out = np.zeros(canvas, dtype=np.uint8)
    y_off = (h_c - new_h) // 2
    x_off = (w_c - new_w) // 2
    out[y_off:y_off + new_h, x_off:x_off + new_w] = clean
    return out


def _dilate(mask: np.ndarray, px: int) -> np.ndarray:
    if px <= 0:
        return mask.astype(bool)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    dilated = cv2.dilate(mask.astype(np.uint8), kernel)
    return dilated.astype(bool)


def _column_projection_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compare ink distribution per column. Catches transposition typos.

    For 'Marketing' vs 'Marketnig' the IoU/Chamfer are nearly identical (same
    letters, same total ink), but the per-column ink mass shifts at the
    transposed positions. L1 distance between normalized column histograms
    captures that.
    """
    proj_a = a.sum(axis=0).astype(np.float64)
    proj_b = b.sum(axis=0).astype(np.float64)
    if proj_a.sum() == 0 or proj_b.sum() == 0:
        return 0.0 if proj_a.sum() != proj_b.sum() else 1.0
    proj_a /= proj_a.sum()
    proj_b /= proj_b.sum()
    # L1 distance in [0, 2]; normalize to [0, 1] similarity
    l1 = float(np.abs(proj_a - proj_b).sum())
    return max(0.0, 1.0 - l1 / 2.0)


def _symmetric_chamfer(a: np.ndarray, b: np.ndarray) -> float:
    """Mean of (avg distance from a-points to nearest b, and vice versa).

    Uses OpenCV distance transform on the inverse mask: the value at any
    pixel is the distance to the nearest foreground pixel of the source.
    """
    if a.sum() == 0 or b.sum() == 0:
        # Degenerate — return large penalty if exactly one side is empty.
        if a.sum() == 0 and b.sum() == 0:
            return 0.0
        return 999.0

    dist_to_b = cv2.distanceTransform((~b.astype(bool)).astype(np.uint8), cv2.DIST_L2, 3)
    dist_to_a = cv2.distanceTransform((~a.astype(bool)).astype(np.uint8), cv2.DIST_L2, 3)

    a_to_b = dist_to_b[a > 0].mean()
    b_to_a = dist_to_a[b > 0].mean()
    return float((a_to_b + b_to_a) / 2.0)
