# Google Batch Semaphore — STOP CONDITION

## Date: 2026-02-26

## STOP CONDITION TRIGGERED

The task requires wrapping each Google batch chunk in:

```python
async with _ENGINE_SEMAPHORE:
    results = await asyncio.to_thread(google_batch_annotate_images, chunk)
```

where `_ENGINE_SEMAPHORE` is the **existing** module-level `asyncio.Semaphore` in
`app/pipeline/ocr_dispatcher.py`.

**`_ENGINE_SEMAPHORE` does not exist in `app/pipeline/ocr_dispatcher.py`.**

---

## Exact Blocking Point

**File:** `app/pipeline/ocr_dispatcher.py`  
**Function:** module level (no per-engine concurrency control exists)

The current file contains:
- `dispatch_zone_ocr(zone, image_bytes)` — sync, no semaphore
- `ZoneEngineResult` class
- Module-level import of `run_ocr_multi`

There is **no `_ENGINE_SEMAPHORE`**, no `asyncio.Semaphore`, and no per-engine
concurrency limit anywhere in the codebase.

The codexV3 prerequisites stated:
> module-level `_ENGINE_SEMAPHORE = asyncio.Semaphore(3)`

This was **never merged into `main`**. The dispatcher on `main` is the original
Phase 4 synchronous implementation with no semaphore.

---

## Minimal Diff to Unblock (≤15 lines)

Add `_ENGINE_SEMAPHORE` to `app/pipeline/ocr_dispatcher.py`:

```python
# app/pipeline/ocr_dispatcher.py — ADD at module level (after imports, ~line 22)
import asyncio

# Per-engine concurrency limit: at most 3 engine calls in flight at once.
_ENGINE_SEMAPHORE = asyncio.Semaphore(3)
```

That is **3 lines** (import + blank + assignment). This is the entire change needed
to unblock the semaphore integration.

Once this exists, `run_routes.py` can import it:

```python
from app.pipeline.ocr_dispatcher import _ENGINE_SEMAPHORE
```

And `_prefetch_google_batch_async` can use it:

```python
async with _ENGINE_SEMAPHORE:
    results = await asyncio.to_thread(google_batch_annotate_images, chunk_bytes)
```

---

## Why the Semaphore Cannot Be Added in run_routes.py or ocr.py

The task specifies:
> Use the EXISTING module-level semaphore from `app/pipeline/ocr_dispatcher.py`  
> DO NOT create any new semaphore

Creating `_ENGINE_SEMAPHORE` in `run_routes.py` or `ocr.py` would be a **new**
semaphore, which is explicitly prohibited. It must live in `ocr_dispatcher.py`
so the dispatcher's own future per-engine calls and the batch path share the same limit.

---

## Why ocr_dispatcher.py Is Not in the Allowed Files List

The task's allowed files are:
- `app/pipeline/run_routes.py`
- `app/ocr.py`
- `tests/*` / `app/tests/*`

`app/pipeline/ocr_dispatcher.py` is **not listed**. Adding `_ENGINE_SEMAPHORE` to it
requires relaxing this constraint.

---

## Proposed Resolution

Choose one:

**Option A — Expand allowed files (minimal):**  
Allow modification of `app/pipeline/ocr_dispatcher.py` for the sole purpose of
adding the 3-line `_ENGINE_SEMAPHORE` declaration. No other changes.

**Option B — Define semaphore in a shared constants module:**  
Create `app/pipeline/engine_config.py` with just:
```python
import asyncio
_ENGINE_SEMAPHORE = asyncio.Semaphore(3)
```
Both `ocr_dispatcher.py` and `run_routes.py` import from there. Keeps dispatcher
unchanged. Requires one new file.

**Option C — Defer semaphore integration:**  
Merge the current `claudeV3-batch-impl` PR (which adds batching without semaphore),
then handle semaphore in a separate task after codexV3 with its semaphore is merged.

---

## Current State Summary

| Item | Status |
|------|--------|
| `_ENGINE_SEMAPHORE` in `ocr_dispatcher.py` | ❌ Does not exist |
| `asyncio.Semaphore` anywhere in codebase | ❌ Not present |
| `google_batch_annotate_images` in `ocr.py` | ✅ PR #13 adds this |
| Cache injection in `run_routes.py` | ✅ PR #13 adds this |
| Per-zone dispatch (`dispatch_zone_ocr`) | ✅ Exists, sync, no semaphore |

**Awaiting decision before implementation.**
