"""
File-based template store.
Templates are stored as JSON files in data/templates/{template_name}.json
Writes are atomic (write to temp file, then rename).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional

from .models import TemplateDef

# Resolve data/templates relative to repo root (2 levels up from this file)
_STORE_DIR = Path(__file__).parent.parent.parent / "data" / "templates"


def _ensure_dir() -> Path:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    return _STORE_DIR


def _path(name: str) -> Path:
    return _ensure_dir() / f"{name}.json"


def list_templates() -> List[str]:
    d = _ensure_dir()
    return sorted(p.stem for p in d.glob("*.json"))


def get_template(name: str) -> Optional[TemplateDef]:
    p = _path(name)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return TemplateDef(**data)


def create_template(template: TemplateDef) -> TemplateDef:
    p = _path(template.template_name)
    if p.exists():
        raise FileExistsError(f"Template '{template.template_name}' already exists")
    template = template.with_timestamps(update=False)
    _atomic_write(p, template)
    return template


def update_template(name: str, template: TemplateDef) -> TemplateDef:
    p = _path(name)
    if not p.exists():
        raise FileNotFoundError(f"Template '{name}' not found")
    # Preserve original created_at if present
    existing = get_template(name)
    data = template.model_dump()
    if existing and existing.created_at_utc:
        data["created_at_utc"] = existing.created_at_utc
    template = TemplateDef(**data).with_timestamps(update=True)
    _atomic_write(p, template)
    return template


def delete_template(name: str) -> None:
    p = _path(name)
    if not p.exists():
        raise FileNotFoundError(f"Template '{name}' not found")
    p.unlink()


def _atomic_write(path: Path, template: TemplateDef) -> None:
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
