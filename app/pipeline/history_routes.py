"""
DEPRECATED. Run history and the review flow have been removed.

This module previously exposed:
  - GET  /api/templates/{name}/history
  - POST /api/runs/{run_id}/zones/{zone_index}/review

Neither endpoint is part of the supported product surface anymore. The
router is kept (empty) so `main.py`'s `app.include_router(history_router)`
still works without a code change, but it registers no routes. Remove
via `git rm` and a follow-up edit in `main.py` when convenient.
"""
from fastapi import APIRouter

history_router = APIRouter()
