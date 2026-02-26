# Google Vision Batching v2 — STOP CONDITION

## Date: 2026-02-26

## STOP CONDITION TRIGGERED

Implementing queue-based async batching (as specified) would break the **dispatcher-thread invariant** without modifying `dispatch_zone_ocr` or its call site in `run_routes.py`.

---

## Exact Blocking Mechanism

### Current execution flow

```
run_routes.py  (async event loop)
  └─ await asyncio.to_thread(dispatch_zone_ocr, zone, zone_bytes)
                                │
                    ┌───────────▼──────────────┐
                    │  Thread pool worker       │
                    │  dispatch_zone_ocr()      │
                    │    for engine_name:       │
                    │      run_ocr_multi(...)   │  ← sync call
                    └───────────────────────────┘
```

`run_ocr_multi` executes **inside a thread pool worker**, not on the asyncio event loop thread.

### Why asyncio.Future/Queue cannot work here

The spec requires:
```python
_GOOGLE_BATCH_QUEUE: list[tuple[bytes, asyncio.Future]]
_GOOGLE_BATCH_LOCK = asyncio.Lock()
_GOOGLE_BATCH_FLUSH_TASK: Optional[asyncio.Task]
```

Each of these is **event-loop-bound**:

| Object | Problem from thread |
|--------|--------------------|
| `asyncio.Future` | Cannot be awaited from a thread. `future.set_result()` must be called via `loop.call_soon_threadsafe()` — requires explicit loop reference |
| `asyncio.Lock` | Cannot be acquired from a thread — `await lock.acquire()` is coroutine-only |
| `asyncio.Task` | `asyncio.ensure_future()` / `asyncio.create_task()` from a thread requires `loop.call_soon_threadsafe(asyncio.create_task, ...)` — unsafe and undocumented |
| flush scheduling | A 5ms `asyncio.sleep` coalesce delay requires an event loop coroutine — not callable from a thread |

The only correct way to bridge thread → event loop is:
```python
loop = asyncio.get_event_loop()  # fragile — loop may differ from worker thread
future = loop.run_until_complete(...)  # DEADLOCK — already inside running loop
```
or
```python
asyncio.run_coroutine_threadsafe(coro, loop)  # requires explicit loop reference
```

Both patterns require **structural changes** to the call site:
- `dispatch_zone_ocr` would need to become `async` and call `await run_ocr_multi_async(...)`, OR
- `run_routes.py` would need to pass the event loop reference down to the OCR layer

Either change **violates the invariants** (dispatcher signature, `done++`, `to_thread` usage).

---

## Exact File and Function

| File | Function | Issue |
|------|----------|-------|
| `app/pipeline/ocr_dispatcher.py` | `dispatch_zone_ocr(zone, image_bytes)` | Sync function — runs in thread pool. Cannot use asyncio primitives. |
| `app/pipeline/run_routes.py` | `run_template` | `await asyncio.to_thread(dispatch_zone_ocr, ...)` — this is the thread boundary. Changing to direct `await` would require async dispatcher. |
| `app/ocr.py` | `run_ocr_multi` | Sync function. Queue-based async batching requires it to become async or use `run_coroutine_threadsafe`. |

---

## Minimal Diff That Would Unblock (≤15 lines)

Only ONE change would make async batching safe without touching dispatcher completion logic:

**Convert `dispatch_zone_ocr` to async and call `run_ocr_multi` via await:**

```python
# app/pipeline/ocr_dispatcher.py — PROPOSED (≤15 lines changed)

# Line 1: change signature
async def dispatch_zone_ocr(zone: ZoneDef, image_bytes: bytes) -> List[ZoneEngineResult]:

# Line 2: change inner call for google engine
    ocr_map = await run_ocr_multi_async(image_bytes, [engine_name])  # new async wrapper

# Line 3: fallback for non-google engines (unchanged, sync wrapped)
    ocr_map = run_ocr_multi(image_bytes, [engine_name])  # azure / ocrspace stay sync
```

And in `run_routes.py` (currently prohibited):
```python
# BEFORE (prohibited from changing):
engine_results = await asyncio.to_thread(dispatch_zone_ocr, zone, zone_bytes)

# AFTER (would be needed):
engine_results = await dispatch_zone_ocr(zone, zone_bytes)
```

This change is **2 lines in run_routes.py** + **async wrapper in ocr.py**, but `run_routes.py` is **prohibited** from modification under the current contract.

---

## What IS Safe to Implement Today (Without Approval)

### Thread-safe synchronous batching via `concurrent.futures`

A purely synchronous approach using a module-level `threading.Lock` and batch accumulator works correctly from thread pool workers:

```python
import threading, math
from concurrent.futures import Future as ThreadFuture

_GOOGLE_BATCH_QUEUE: list[tuple[bytes, ThreadFuture]] = []
_GOOGLE_BATCH_LOCK = threading.Lock()
CHUNK_SIZE = 16
```

Behavior: first thread to arrive with a full batch (or after a short spin-wait) executes the batch; other threads block on `future.result()`. This preserves all invariants:
- Sync call signature of `run_ocr_multi` unchanged ✅
- `dispatch_zone_ocr` unchanged ✅  
- `run_routes.py` unchanged ✅
- `done++` semantics unchanged ✅
- Semaphore: batch acquires `_ENGINE_SEMAPHORE` once via the designated leader thread ✅

However, this approach **requires a synchronous semaphore** (not `asyncio.Semaphore`) for the batch leader, which conflicts with the spec's `_ENGINE_SEMAPHORE = asyncio.Semaphore(3)` — an asyncio semaphore cannot be acquired from a thread.

---

## Summary

| Approach | Invariants preserved | Feasible without rule changes |
|----------|---------------------|-------------------------------|
| Async queue (`asyncio.Future` + `asyncio.Lock`) | ❌ requires async dispatcher | ❌ NO |
| Thread-safe sync batch (`threading.Lock`) | ✅ all invariants | ⚠️ asyncio semaphore conflict |
| Add `_ocr_google_batch` helper only (no wiring) | ✅ all invariants | ✅ YES |

**Recommendation:** Either (A) allow `run_routes.py` to remove the `asyncio.to_thread` wrapper so dispatcher can become async-native, OR (B) scope the work to adding `_ocr_google_batch` as an untriggered helper + full unit tests, deferring wiring to a future phase.

---

## Cloud Verification Commands

```bash
# Build container
gcloud builds submit \
  --project=project-d245d8c8-8548-47d2-a04 \
  --region=europe-west1

# Deploy service  
gcloud run deploy ai-ocr \
  --project=project-d245d8c8-8548-47d2-a04 \
  --region=europe-west1 \
  --image=gcr.io/project-d245d8c8-8548-47d2-a04/ai-ocr:latest

# Check for batch logs
gcloud logging read \
  'resource.type="cloud_run_revision" AND textPayload:"google_batch_v2 size="' \
  --project=project-d245d8c8-8548-47d2-a04 \
  --limit=20
```

**Execution not performed in this environment.**
