"""
Template store: Firestore-backed with fallback to local file store.

Strategy (A1):
  1. Try Firestore (when FIRESTORE_AVAILABLE=true and library present).
  2. Fallback to local file store (data/templates/*.json).
  3. On successful file-fallback read → auto-import to Firestore with log.

All writes are atomic at the single-template level.
Firestore model:
  collection: templates
  doc id:     template_name
  fields:     template_name, template_json (str), updated_at (UTC Z), created_at (UTC Z)
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.logging_utils import log_event
from .models import TemplateDef
from .firestore_store import is_persistence_enabled, get_db

# ── Local file store (fallback) ───────────────────────────────────────────────
_STORE_DIR = Path(__file__).parent.parent.parent / "data" / "templates"


def _ensure_dir() -> Path:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    return _STORE_DIR


def _local_path(name: str) -> Path:
    return _ensure_dir() / f"{name}.json"


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Firestore helpers ─────────────────────────────────────────────────────────
_COLLECTION = "templates"


def _fs_get(name: str) -> Optional[TemplateDef]:
    try:
        db = get_db()
        doc = db.collection(_COLLECTION).document(name).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        return TemplateDef(**json.loads(data["template_json"]))
    except Exception as exc:
        log_event("template_store_error", op="fs_get",
                  exc_type=type(exc).__name__, message=str(exc))
        return None


def _fs_list() -> List[str]:
    try:
        db = get_db()
        docs = db.collection(_COLLECTION).stream()
        return sorted(d.id for d in docs)
    except Exception as exc:
        log_event("template_store_error", op="fs_list",
                  exc_type=type(exc).__name__, message=str(exc))
        return []


def _fs_set(template: TemplateDef) -> None:
    """Atomic set (create or overwrite) a template doc in Firestore."""
    db = get_db()
    now = _now_utc_str()
    payload = {
        "template_name": template.template_name,
        "template_json": template.model_dump_json(),
        "updated_at": now,
        "created_at": getattr(template, "created_at_utc", None) or now,
    }
    db.collection(_COLLECTION).document(template.template_name).set(payload)


def _fs_delete(name: str) -> None:
    db = get_db()
    db.collection(_COLLECTION).document(name).delete()


# ── Local file helpers ────────────────────────────────────────────────────────

def _local_list() -> List[str]:
    d = _ensure_dir()
    return sorted(p.stem for p in d.glob("*.json"))


def _local_get(name: str) -> Optional[TemplateDef]:
    p = _local_path(name)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return TemplateDef(**data)


def _local_atomic_write(path: Path, template: TemplateDef) -> None:
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(template.model_dump_json(indent=2))
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Auto-import helper ────────────────────────────────────────────────────────

def _auto_import_to_firestore(template: TemplateDef) -> None:
    """Best-effort: import template from file store into Firestore and log."""
    try:
        _fs_set(template)
        log_event("template_auto_imported",
                  template_name=template.template_name,
                  source="file_fallback")
    except Exception as exc:
        log_event("template_auto_import_failed",
                  template_name=template.template_name,
                  exc_type=type(exc).__name__, message=str(exc))


# ── Public CRUD interface (same as before — callers unchanged) ────────────────

def list_templates() -> List[str]:
    if is_persistence_enabled():
        return sorted(set(_fs_list()) | set(_local_list()))
    return _local_list()


def get_template(name: str) -> Optional[TemplateDef]:
    if is_persistence_enabled():
        tmpl = _fs_get(name)
        if tmpl is not None:
            return tmpl
        # Fallback to file store
        tmpl = _local_get(name)
        if tmpl is not None:
            _auto_import_to_firestore(tmpl)
        return tmpl
    return _local_get(name)


def create_template(template: TemplateDef) -> TemplateDef:
    template = template.with_timestamps(update=False)
    if is_persistence_enabled():
        existing = _fs_get(template.template_name)
        if existing is not None:
            raise FileExistsError(f"Template '{template.template_name}' already exists")
        _fs_set(template)
        return template
    # Local file store
    p = _local_path(template.template_name)
    if p.exists():
        raise FileExistsError(f"Template '{template.template_name}' already exists")
    _local_atomic_write(p, template)
    return template


def update_template(name: str, template: TemplateDef) -> TemplateDef:
    if is_persistence_enabled():
        existing = _fs_get(name)
        if existing is None:
            raise FileNotFoundError(f"Template '{name}' not found")
        data = template.model_dump()
        if existing.created_at_utc:
            data["created_at_utc"] = existing.created_at_utc
        template = TemplateDef(**data).with_timestamps(update=True)
        _fs_set(template)
        return template
    # Local file store
    p = _local_path(name)
    if not p.exists():
        raise FileNotFoundError(f"Template '{name}' not found")
    existing = _local_get(name)
    data = template.model_dump()
    if existing and existing.created_at_utc:
        data["created_at_utc"] = existing.created_at_utc
    template = TemplateDef(**data).with_timestamps(update=True)
    _local_atomic_write(p, template)
    return template


def delete_template(name: str) -> None:
    if is_persistence_enabled():
        existing = _fs_get(name)
        if existing is None:
            raise FileNotFoundError(f"Template '{name}' not found")
        _fs_delete(name)
        return
    p = _local_path(name)
    if not p.exists():
        raise FileNotFoundError(f"Template '{name}' not found")
    p.unlink()
