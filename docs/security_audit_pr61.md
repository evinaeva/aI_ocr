# Deep Security & Correctness Audit — PR #61 OCR Crop Invariant

Date: 2026-04-22

## Verdict

❌ VULNERABILITIES FOUND

## Vulnerability 1 — CroppedImage metadata spoof allows full-image bytes to OCR

- **File / function:** `app/pipeline/cropped_image.py` → `CroppedImage.__post_init__`
- **Code path:** `dispatch_zone_ocr()` only checks `isinstance(CroppedImage)` then sends `cropped_image.bytes` to `run_ocr_multi()`.
  - `app/pipeline/ocr_dispatcher.py` lines 98-103 and 110-111.
- **Break mechanism:** `CroppedImage` validates area using caller-supplied `crop_width/crop_height`, not actual decoded dimensions of `bytes`. It also only compares hash to original when `original_sha256` is provided.
  - So an attacker/developer can construct `CroppedImage(bytes=<full image>, bbox=[1,1,2,2], crop_width=1, crop_height=1, original_sha256=None, cropped=True)` and pass checks.
- **Exploit scenario:** Any alternate/internal code path that manually constructs `CroppedImage` (instead of `make_cropped_image`) can inject full-image payloads while appearing as a valid crop.
- **Severity:** HIGH

## Vulnerability 2 — Geometry near-full rejection is environment-tunable up to 100%

- **File / function:** `app/pipeline/cropped_image.py` → `CroppedImage.__post_init__`
- **Code path:** `OCR_MAX_CROP_AREA_RATIO` env var controls the near-full rejection threshold.
- **Break mechanism:** Setting `OCR_MAX_CROP_AREA_RATIO=1.0` permits 99%-area crops, violating strict near-full rejection requirements.
- **Exploit scenario:** Misconfiguration (or intentional config tampering) allows oversized crops that are effectively full-image extracts except thin borders.
- **Severity:** MEDIUM

## Required fixes (minimal)

1. In `CroppedImage.__post_init__`, decode `self.bytes` and assert decoded `(width, height)` equals `(self.crop_width, self.crop_height)`.
2. In `CroppedImage.__post_init__`, make `original_sha256` mandatory and reject when missing.
3. In `CroppedImage.__post_init__`, enforce immutable max crop ratio in code (`0.95`) or clamp env overrides to at most `0.95`.
4. In `dispatch_zone_ocr`, add a secondary assertion that `cropped_image.original_sha256` is present and `cropped_image.crop_width * cropped_image.crop_height` is below hard cap before sending payload.

## Confirmed fail-closed behaviors that are currently correct

- ZIP session flow marks missing bbox and crop failures as manual; no OCR fallback to original image.
- `debug_ocr` requires manifest bbox; missing or crop failure does not OCR.
- Batch pipeline crops before OCR and dispatches `CroppedImage` only.
- `dispatch_zone_ocr` rejects non-`CroppedImage` payloads.

