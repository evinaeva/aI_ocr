"""Skeletonize an image region to bare text structure.

The point: banners have shadows, glow, outlines, gradient fills, photo backgrounds.
We strip all that and leave a 1px-thick skeleton — which depends only on the
glyph shape, not on decorative effects.

Pipeline:
  RGBA/RGB -> alpha- or contrast-aware binary mask -> skeletonize -> uint8 array.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter
from skimage.morphology import closing as sk_closing, skeletonize as sk_skeletonize


def to_binary(
    img: Image.Image,
    invert: bool | None = None,
    *,
    pre_blur_sigma: float = 0.0,
) -> np.ndarray:
    """Convert an image (text on background) to a clean binary mask.

    If `img` is RGBA with a transparent background, use the alpha channel.
    Otherwise convert to grayscale + Otsu-style threshold.

    `invert=None` auto-picks based on the mode and what looks like the
    foreground; `True`/`False` overrides.

    `pre_blur_sigma > 0` applies a Gaussian blur before thresholding —
    helps on multi-colour gradient text / decorative effects where the
    naive Otsu polarity is dominated by background noise. Recommended
    range 0.5–1.0 on real banners (see analysis 2026-05-26).
    """
    if img.mode == "RGBA":
        arr = np.array(img)
        alpha = arr[..., 3]
        # If alpha is mostly opaque, this image was flattened (banner crop or
        # composited render). Alpha is useless as a foreground mask — fall
        # through to the grayscale path.
        if (alpha < 250).mean() > 0.02:
            mask = alpha > 128
            return mask.astype(np.uint8)

    gray_img = img.convert("L")
    if pre_blur_sigma > 0:
        gray_img = gray_img.filter(ImageFilter.GaussianBlur(radius=pre_blur_sigma))
    gray = np.array(gray_img)

    # Simple Otsu via numpy. For real banners with heavy effects, callers
    # may want adaptive thresholding instead — currently kept simple.
    thresh = _otsu_threshold(gray)
    fg = gray < thresh  # assume darker text on lighter bg
    if invert is True:
        fg = ~fg
    elif invert is None:
        # auto: pick the polarity with fewer foreground pixels (text is usually
        # minority on a banner crop). If both look big, leave as-is.
        if fg.mean() > 0.5:
            fg = ~fg
    return fg.astype(np.uint8)


def skeletonize(binary: np.ndarray, *, close: bool = True) -> np.ndarray:
    """Run scikit-image skeletonize on a binary mask. Returns uint8 (0/1).

    `close=True` (default) applies a small binary_closing after skeletonize
    to bridge 1–2 px gaps that appear when decorative effects make Otsu
    produce a torn mask. Recommendation from repo analysis 2026-05-26.
    Disable when comparing already-clean renders where gap-filling could
    obscure a real "missing dot on i".
    """
    if binary.dtype != bool:
        binary = binary.astype(bool)
    sk = sk_skeletonize(binary)
    if close:
        sk = sk_closing(sk)
    return sk.astype(np.uint8)


def extract_skeleton(
    img: Image.Image,
    invert: bool | None = None,
    *,
    pre_blur_sigma: float = 0.0,
    close: bool = True,
) -> np.ndarray:
    """Full pipeline: image -> binary -> skeleton.

    See `to_binary` for `pre_blur_sigma` and `skeletonize` for `close`.
    """
    binary = to_binary(img, invert=invert, pre_blur_sigma=pre_blur_sigma)
    return skeletonize(binary, close=close)


def _otsu_threshold(gray: np.ndarray) -> int:
    """Otsu's threshold. Returns the chosen threshold value."""
    hist, _ = np.histogram(gray, bins=256, range=(0, 256))
    total = gray.size
    sum_total = (np.arange(256) * hist).sum()
    sum_b = 0.0
    weight_b = 0
    max_var = 0.0
    threshold = 127
    for t in range(256):
        weight_b += hist[t]
        if weight_b == 0:
            continue
        weight_f = total - weight_b
        if weight_f == 0:
            break
        sum_b += t * hist[t]
        mean_b = sum_b / weight_b
        mean_f = (sum_total - sum_b) / weight_f
        var = weight_b * weight_f * (mean_b - mean_f) ** 2
        if var > max_var:
            max_var = var
            threshold = t
    return threshold
