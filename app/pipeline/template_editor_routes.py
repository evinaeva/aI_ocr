"""
Template Editor UI route — Phase 2.
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..logging_utils import log_event

BASE_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

editor_router = APIRouter()


@editor_router.get("/templates/editor", response_class=HTMLResponse)
async def template_editor(request: Request):
    log_event("template_editor_open")
    return templates.TemplateResponse("template_editor.html", {"request": request})
