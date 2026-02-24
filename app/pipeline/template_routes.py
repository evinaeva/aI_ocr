"""
CRUD API endpoints for template management.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .models import TemplateDef
from . import template_store as store
from ..logging_utils import log_event

router = APIRouter(prefix="/api/templates", tags=["templates"])


def _validation_error(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": "validation_error", "details": detail},
    )


@router.get("", response_class=JSONResponse)
async def list_templates() -> JSONResponse:
    names = store.list_templates()
    return JSONResponse({"templates": names})


@router.get("/{name}", response_class=JSONResponse)
async def get_template(name: str) -> JSONResponse:
    tmpl = store.get_template(name)
    if tmpl is None:
        return JSONResponse({"error": "not_found", "details": f"Template '{name}' not found"}, status_code=404)
    return JSONResponse(tmpl.model_dump())


@router.post("", response_class=JSONResponse)
async def create_template(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception as exc:
        return _validation_error(f"Invalid JSON: {exc}")

    try:
        tmpl = TemplateDef(**body)
    except ValidationError as exc:
        return _validation_error(str(exc))

    try:
        created = store.create_template(tmpl)
    except FileExistsError as exc:
        return JSONResponse(
            status_code=409,
            content={"error": "conflict", "details": str(exc)},
        )

    log_event("template_created", template_name=created.template_name)
    return JSONResponse(created.model_dump(), status_code=201)


@router.put("/{name}", response_class=JSONResponse)
async def update_template(name: str, request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception as exc:
        return _validation_error(f"Invalid JSON: {exc}")

    try:
        tmpl = TemplateDef(**body)
    except ValidationError as exc:
        return _validation_error(str(exc))

    if tmpl.template_name != name:
        return _validation_error(
            f"URL name '{name}' does not match body template_name '{tmpl.template_name}'"
        )

    try:
        updated = store.update_template(name, tmpl)
    except FileNotFoundError as exc:
        return JSONResponse({"error": "not_found", "details": str(exc)}, status_code=404)

    log_event("template_updated", template_name=updated.template_name)
    return JSONResponse(updated.model_dump())


@router.delete("/{name}", response_class=JSONResponse)
async def delete_template(name: str) -> JSONResponse:
    try:
        store.delete_template(name)
    except FileNotFoundError as exc:
        return JSONResponse({"error": "not_found", "details": str(exc)}, status_code=404)

    log_event("template_deleted", template_name=name)
    return JSONResponse({"ok": True, "deleted": name})
