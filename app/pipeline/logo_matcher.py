"""
Logo template matching module.

All configuration is centralized here (single source of truth per SpecLock §10).
Never calls OCR engines — logo zones bypass the OCR dispatcher entirely.

GCS loading:
  Templates are stored in GCS under:
    gs://<LOGO_TEMPLATES_GCS_BUCKET>/<LOGO_TEMPLATES_GCS_PREFIX>/<brand_or_set_name>/
  Files with extensions .png or .jpg are treated as logo templates.
  Templates are cached on disk under LOGO_TEMPLATES_CACHE_DIR (/tmp/logo_templates
  by default) to avoid repeated GCS reads within the same instance lifetime.

Algorithm (multi-scale TM_CCOEFF_NORMED in grayscale):
  For each template image T and for each scale factor in LOGO_MATCH_SCALES:
    1. Resize T to target_h = int(T.h * scale), target_w = int(T.w * scale).
    2. Skip if resized template is larger than the zone crop in either dimension.
    3. Run cv2.matchTemplate(zone_gray, resized_T_gray, TM_CCOEFF_NORMED).
    4. Record max score.
  Final score = max score across all (template × scale) combinations.
  score >= LOGO_MATCH_THRESHOLD → OK, else MANUAL.

Backward compatibility:
  If engine_config contains logo_template_base64 or logo_templates_base64,
  those embedded templates are used directly (no GCS call). This allows
  existing single-template zone configs to keep working without migration.
"""
from __future__ import annotations

import io
import logging
import os
import threading
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Centralised configuration — all tunable knobs live here (SpecLock §10)
# ---------------------------------------------------------------------------

# GCS bucket that stores logo template images.
LOGO_TEMPLATES_GCS_BUCKET: str = os.getenv("LOGO_TEMPLATES_GCS_BUCKET", "")

# Prefix (folder) inside the bucket.
LOGO_TEMPLATES_GCS_PREFIX: str = os.getenv(
    "LOGO_TEMPLATES_GCS_PREFIX", "logo_templates"
)

# Local cache directory for downloaded GCS templates.
LOGO_TEMPLATES_CACHE_DIR: str = os.getenv(
    "LOGO_TEMPLATES_CACHE_DIR", "/tmp/logo_templates"
)

# Match threshold: score >= LOGO_MATCH_THRESHOLD → zone status OK.
_raw_threshold = os.getenv("LOGO_MATCH_THRESHOLD", "0.7").strip()
try:
    LOGO_MATCH_THRESHOLD: float = float(_raw_threshold)
except ValueError:
    LOGO_MATCH_THRESHOLD = 0.7

# Scale factors applied to each template during multi-scale matching.
# Covers shrunk and enlarged versions; step kept deterministic.
LOGO_MATCH_SCALES: tuple[float, ...] = (0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4)

# OpenCV matching method — fixed, never scattered.
_CV_METHOD = cv2.TM_CCOEFF_NORMED

# Minimum template side length after scaling (pixels). Templates smaller than
# this after resize are skipped to avoid noise from degenerate matches.
_MIN_TPL_SIDE = 8

# ---------------------------------------------------------------------------
# GCS template loading with on-disk cache
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_MEMORY_CACHE: dict[str, list[np.ndarray]] = {}  # set_key → list of gray images


def _gcs_client():
    """Lazy import of GCS client to avoid hard dependency at module load."""
    from google.cloud import storage  # type: ignore
    return storage.Client()


def _load_templates_from_gcs(set_key: str) -> list[np.ndarray]:
    """
    Download all .png/.jpg files under
      gs://<bucket>/<prefix>/<set_key>/
    into LOGO_TEMPLATES_CACHE_DIR and return as list of grayscale ndarrays.

    Returns empty list if bucket not configured or on any error.
    """
    if not LOGO_TEMPLATES_GCS_BUCKET:
        return []

    cache_dir = Path(LOGO_TEMPLATES_CACHE_DIR) / set_key
    prefix = f"{LOGO_TEMPLATES_GCS_PREFIX}/{set_key}/"

    try:
        client = _gcs_client()
        bucket = client.bucket(LOGO_TEMPLATES_GCS_BUCKET)
        blobs = list(bucket.list_blobs(prefix=prefix))
        image_blobs = [
            b for b in blobs
            if b.name.lower().endswith((".png", ".jpg", ".jpeg"))
        ]
        if not image_blobs:
            logger.warning(
                "logo_gcs_no_templates bucket=%s prefix=%s",
                LOGO_TEMPLATES_GCS_BUCKET, prefix,
            )
            return []

        cache_dir.mkdir(parents=True, exist_ok=True)
        templates: list[np.ndarray] = []
        for blob in image_blobs:
            local_path = cache_dir / Path(blob.name).name
            if not local_path.exists():
                blob.download_to_filename(str(local_path))
                logger.info("logo_gcs_downloaded blob=%s local=%s", blob.name, local_path)
            img_gray = _load_gray(local_path.read_bytes())
            if img_gray is not None:
                templates.append(img_gray)

        logger.info(
            "logo_gcs_loaded set_key=%s count=%d", set_key, len(templates)
        )
        return templates

    except Exception as exc:
        logger.warning("logo_gcs_load_failed set_key=%s err=%s", set_key, exc)
        return []


def get_gcs_templates(set_key: str) -> list[np.ndarray]:
    """Return cached grayscale templates for *set_key*, loading from GCS on first call."""
    with _CACHE_LOCK:
        if set_key in _MEMORY_CACHE:
            return _MEMORY_CACHE[set_key]
        templates = _load_templates_from_gcs(set_key)
        _MEMORY_CACHE[set_key] = templates
        return templates


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _load_gray(data: bytes) -> Optional[np.ndarray]:
    """Decode image bytes to grayscale ndarray; return None on failure."""
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    return img  # may be None if decoding fails


def _decode_b64(b64_str: str) -> Optional[np.ndarray]:
    """Decode a base64 string to a grayscale ndarray; return None on failure."""
    import base64
    try:
        data = base64.b64decode(b64_str)
        return _load_gray(data)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Multi-scale template matching core
# ---------------------------------------------------------------------------

def _multiscale_score(zone_gray: np.ndarray, tpl_gray: np.ndarray) -> float:
    """
    Return the best TM_CCOEFF_NORMED score for *tpl_gray* matched against
    *zone_gray* across all scale factors in LOGO_MATCH_SCALES.

    The template is resized; the zone crop is never resized (it is what it is).
    """
    best = 0.0
    zone_h, zone_w = zone_gray.shape[:2]

    for scale in LOGO_MATCH_SCALES:
        new_h = max(1, int(round(tpl_gray.shape[0] * scale)))
        new_w = max(1, int(round(tpl_gray.shape[1] * scale)))

        # Skip if template (after scale) would not fit in the zone.
        if new_h > zone_h or new_w > zone_w:
            continue
        # Skip degenerate tiny templates.
        if new_h < _MIN_TPL_SIDE or new_w < _MIN_TPL_SIDE:
            continue

        resized = cv2.resize(tpl_gray, (new_w, new_h), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(zone_gray, resized, _CV_METHOD)
        _, max_val, _, _ = cv2.minMaxLoc(result)
        score = float(max_val)
        if score > best:
            best = score
        if best >= LOGO_MATCH_THRESHOLD:
            # Early exit once threshold met.
            return best

    return best


# ---------------------------------------------------------------------------
# Public API: match_logo_zone
# ---------------------------------------------------------------------------

def match_logo_zone(zone_bytes: bytes, engine_config: dict) -> dict:
    """
    Run multi-scale OpenCV template matching for a logo zone.

    Template source priority:
      1. logo_templates_base64 (list of base64 strings) in engine_config.
      2. logo_template_base64 (single base64 string) in engine_config — backward compat.
      3. logo_gcs_set_key (str) in engine_config → loads from GCS bucket.
      4. GCS bucket with set_key = "default" if bucket env var is set.

    Returns a consensus-compatible dict with keys:
      selected_engine, selected_text, rule_used, zone_status, reason, logo_score.
    """
    cfg = engine_config or {}

    # --- Collect template images ---
    templates: list[np.ndarray] = []

    # Priority 1 & 2: embedded base64 templates
    raw_list = cfg.get("logo_templates_base64")
    if isinstance(raw_list, list):
        for item in raw_list:
            img = _decode_b64(str(item).strip())
            if img is not None:
                templates.append(img)
    else:
        single = (cfg.get("logo_template_base64") or "").strip()
        if single:
            img = _decode_b64(single)
            if img is not None:
                templates.append(img)

    # Priority 3 & 4: GCS set_key
    if not templates:
        set_key = (cfg.get("logo_gcs_set_key") or "default").strip()
        templates = get_gcs_templates(set_key)

    def _result(status: str, reason: Optional[str], score: float = 0.0) -> dict:
        return {
            "selected_engine": "opencv",
            "selected_text": "",
            "rule_used": "logo_template",
            "zone_status": status,
            "reason": reason,
            "logo_score": round(score, 4),
        }

    if not templates:
        return _result("MANUAL", "logo_template_missing")

    # --- Decode zone crop to grayscale ---
    zone_gray = _load_gray(zone_bytes)
    if zone_gray is None:
        return _result("MANUAL", "logo_decode_failed")

    # --- Multi-scale matching across all templates ---
    best_score = 0.0
    valid_template_found = False

    for tpl in templates:
        # Skip templates that can't fit at scale=1.0 and won't fit at any
        # smaller scale above the minimum side threshold.
        tpl_h, tpl_w = tpl.shape[:2]
        zone_h, zone_w = zone_gray.shape[:2]
        min_scale = max(
            _MIN_TPL_SIDE / max(1, tpl_h),
            _MIN_TPL_SIDE / max(1, tpl_w),
        )
        # Check if the template can fit in the zone at *some* scale <= 1.0.
        fits_at_some_scale = (
            (tpl_h <= zone_h and tpl_w <= zone_w)  # already fits at scale 1.0
            or any(
                int(round(tpl_h * s)) <= zone_h
                and int(round(tpl_w * s)) <= zone_w
                and int(round(tpl_h * s)) >= _MIN_TPL_SIDE
                and int(round(tpl_w * s)) >= _MIN_TPL_SIDE
                for s in LOGO_MATCH_SCALES
                if s < 1.0
            )
        )
        if not fits_at_some_scale:
            logger.debug(
                "logo_template_skipped tpl_h=%d tpl_w=%d zone_h=%d zone_w=%d",
                tpl_h, tpl_w, zone_h, zone_w,
            )
            continue

        valid_template_found = True
        score = _multiscale_score(zone_gray, tpl)
        if score > best_score:
            best_score = score
        if best_score >= LOGO_MATCH_THRESHOLD:
            break  # early exit

    if not valid_template_found:
        return _result("MANUAL", "logo_template_larger_than_zone")

    threshold = float(cfg.get("logo_match_threshold", LOGO_MATCH_THRESHOLD))
    status = "OK" if best_score >= threshold else "MANUAL"
    reason = None if status == "OK" else "logo_mismatch"
    return _result(status, reason, best_score)
