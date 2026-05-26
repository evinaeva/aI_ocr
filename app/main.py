"""
OCR Localization Checker — FastAPI main application.
"""
import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import math
import os
import sqlite3
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from numbers import Real
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
from urllib.parse import unquote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .logging_utils import log_event
from .metrics.engine_usage import get_current_month_usage, init_engine_usage_metrics
from .metrics.llm_usage import get_current_month_llm_usage, init_llm_usage_metrics
from .normalizer import clean_for_display, normalize_strict
from .ocr import (
    ALL_ENGINES,
    OCRResult,
    _google_cache_clear,
    _google_cache_put,
    emit_startup_warnings,
    google_batch_annotate_images,
)
from .pipeline import template_store
from .pipeline.batch_routes import batch_router  # P2.4: v2-batch job orchestration
from .pipeline.cropped_image import CroppedImage
from .pipeline.models import ZoneDef
from .pipeline.ocr_dispatcher import dispatch_zone_ocr
from .pipeline.preprocessor import load_image, make_cropped_image
from .pipeline.phase2_routes import phase2_router
from .pipeline.preview_routes import preview_router
from .pipeline.run_routes import run_router
from .pipeline.template_editor_routes import editor_router
from .pipeline.template_routes import router as template_router
from .section_matcher import extract_sections, select_best
from .version import APP_VERSION, BUILD_TIME_UTC, get_build_info
from .zip_processor import build_zip_manifest, process_zip

# Banner QA (CV-only, no LLM) — sibling pipeline accessible under /banner.
# The router is loaded lazily-safe: importing this module pulls in PIL +
# numpy + cv2 + skimage, but easyocr/torch are only touched on the first
# detect call, so the OCR-LLM flow is unaffected even if torch isn't
# installed in the image yet.
from .banner_qa.routes import banner_router, DEFAULT_THRESHOLD as BANNER_DEFAULT_THRESHOLD
from .banner_qa.fonts import CATALOG as BANNER_FONT_CATALOG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = os.getenv("DB_PATH", "/tmp/sessions.db")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")

if not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD is required")
if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET is required")

SESSION_COOKIE_NAME = "aiocr_session"
CSRF_COOKIE_NAME = "aiocr_csrf"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            created_at REAL,
            status TEXT,
            total INTEGER DEFAULT 0,
            pass_count INTEGER DEFAULT 0,
            fail_count INTEGER DEFAULT 0,
            manual_count INTEGER DEFAULT 0,
            engines TEXT
        );
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            lang TEXT,
            image_name TEXT,
            text_name TEXT,
            ref_text TEXT,
            section_name TEXT,
            section_number INTEGER,
            status TEXT,
            score REAL,
            reason TEXT,
            manual_decision TEXT,
            ocr_results_json TEXT,
            best_engine TEXT
        );
        CREATE TABLE IF NOT EXISTS images (
            session_id TEXT,
            filename TEXT,
            data BLOB,
            PRIMARY KEY (session_id, filename)
        );
        CREATE TABLE IF NOT EXISTS phase2_uploads (
            upload_id TEXT PRIMARY KEY,
            created_at REAL,
            zip_bytes BLOB,
            section_number INTEGER,
            section_name TEXT
        );
    """
    )
    # Migration: add engines column if missing (for old deployments)
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN engines TEXT")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN session_meta_json TEXT")
        conn.commit()
    except Exception:
        pass
    # Migration: add new columns to results if missing
    for col in [
        "ocr_results_json TEXT",
        "best_engine TEXT",
        "reference_confidence REAL",
        "reference_score_top1 REAL",
        "reference_score_top2 REAL",
        "reference_margin REAL",
        "zone_name TEXT",
        "target_id TEXT",
    ]:
        try:
            conn.execute(f"ALTER TABLE results ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # B6: emit Azure env var warnings once at startup
    emit_startup_warnings()
    init_engine_usage_metrics()
    init_llm_usage_metrics()
    log_event("app_start", app_version=APP_VERSION)
    yield


app = FastAPI(lifespan=lifespan, title="OCR Localization Checker")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(template_router)
app.include_router(editor_router)
app.include_router(preview_router)
app.include_router(run_router)
app.include_router(batch_router)  # P2.4: v2-batch job orchestration (additive)
app.include_router(phase2_router)
app.include_router(banner_router)  # /api/banner/* — local CV pipeline (no LLM)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_sse_queues: Dict[str, asyncio.Queue] = {}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign_payload(payload_b64: str) -> str:
    sig = hmac.new(
        SESSION_SECRET.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64url_encode(sig)


def _make_session_cookie() -> str:
    payload = {"auth": True, "iat": int(time.time())}
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign_payload(payload_b64)}"


def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token or "." not in token:
        return False
    payload_b64, sig = token.split(".", 1)
    expected_sig = _sign_payload(payload_b64)
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return False
    if payload.get("auth") is not True:
        return False
    iat = payload.get("iat")
    if not isinstance(iat, int):
        return False
    now = int(time.time())
    delta = now - iat
    return 0 <= delta <= SESSION_TTL_SECONDS


def _cleanup_phase2_uploads(conn: sqlite3.Connection):
    cutoff = time.time() - SESSION_TTL_SECONDS
    conn.execute("DELETE FROM phase2_uploads WHERE created_at < ?", (cutoff,))


def _read_archive_image(zip_bytes: bytes, archive_path: str) -> bytes:
    parts = archive_path.split("!/")
    current = io.BytesIO(zip_bytes)
    zf = zipfile.ZipFile(current)
    try:
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                return zf.read(part)
            nested_bytes = zf.read(part)
            zf.close()
            current = io.BytesIO(nested_bytes)
            zf = zipfile.ZipFile(current)
    finally:
        zf.close()


def _collect_zip_debug_counters(zip_bytes: bytes) -> dict:
    counters = {
        "zip_entries_total": 0,
        "images_detected_total": 0,
        "images_queued_total": 0,
        "images_processed_total": 0,
        "images_skipped_total": 0,
        "images_skipped_by_reason": {},
    }

    def _walk(zf: zipfile.ZipFile):
        for info in zf.infolist():
            if info.filename.endswith("/"):
                continue
            counters["zip_entries_total"] += 1
            lower = info.filename.lower()
            if lower.endswith(".zip"):
                try:
                    nested = zipfile.ZipFile(io.BytesIO(zf.read(info)))
                except zipfile.BadZipFile:
                    continue
                with nested:
                    _walk(nested)

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            _walk(zf)
    except zipfile.BadZipFile:
        pass
    return counters


def _crop_zip_zone(image_bytes: bytes, bbox: list[int]) -> CroppedImage:
    img = load_image(image_bytes)
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        raise RuntimeError("Zero-area crop — refusing OCR")
    cropped = img.crop((x1, y1, x2, y2))
    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return make_cropped_image(
        image_bytes,
        bbox,
        buf.getvalue(),
        original_width=img.width,
        original_height=img.height,
        crop_width=max(1, x2 - x1),
        crop_height=max(1, y2 - y1),
    )


def _resolve_real_bbox(item: Any) -> Optional[list[int]]:
    direct_bbox = getattr(item, "bbox", None)
    if direct_bbox is None:
        return None
    if isinstance(direct_bbox, (str, bytes, bytearray)):
        return None
    try:
        values = list(direct_bbox)
    except TypeError:
        return None
    if len(values) != 4:
        return None
    normalized: list[int] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, Real):
            return None
        if isinstance(v, float) and not v.is_integer():
            return None
        normalized.append(int(v))
    return normalized


def _select_manifest_item_for_debug(
    manifest_items: List[Any],
    lang: str,
    image_name: str,
) -> Optional[Any]:
    lang_norm = (lang or "").strip().lower()
    image_name_norm = (image_name or "").strip()
    image_basename = image_name_norm.rsplit("/", 1)[-1]
    for item in manifest_items:
        item_lang = (getattr(item, "lang", "") or "").strip().lower()
        item_path = (getattr(item, "archive_path", "") or "").strip()
        if item_lang != lang_norm:
            continue
        if item_path == image_name_norm or item_path.rsplit("/", 1)[-1] == image_basename:
            return item
    for item in manifest_items:
        item_lang = (getattr(item, "lang", "") or "").strip().lower()
        if item_lang == lang_norm:
            return item
    return None


def _run_zone_ocr_for_engines(cropped_image: CroppedImage, engines: List[str]) -> Dict[str, OCRResult]:
    zone = ZoneDef(
        name="zip_item_ocr",
        type="ocr",
        bbox=cropped_image.bbox,
        engines=list(engines),
        engine_config={},
    )
    results = dispatch_zone_ocr(zone, cropped_image)
    out: Dict[str, OCRResult] = {}
    for r in results:
        out[r.engine] = OCRResult(r.text, r.confidence if r.confidence is not None else 0.0, r.engine)
    return out


def _new_csrf_token() -> str:
    return _b64url_encode(os.urandom(32))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()
    is_api = path.startswith("/api/")
    is_public = path.startswith("/static/") or path == "/login"

    if not is_public and not _is_authenticated(request):
        if is_api:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)

    # Skip CSRF check for /login: user has no session cookie yet, so no CSRF cookie exists.
    # Reading the form body here would also consume it, causing FastAPI to see an empty body
    # and raise "Field required" for the password parameter.
    if method == "POST" and not is_api and path != "/login":
        content_type = (request.headers.get("content-type") or "").lower()
        is_form_post = content_type.startswith("application/x-www-form-urlencoded") or content_type.startswith(
            "multipart/form-data"
        )
        if is_form_post:
            csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
            form = await request.form()
            csrf_form = str(form.get("csrf_token", ""))
            if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_form):
                return JSONResponse({"detail": "CSRF failed"}, status_code=403)

    return await call_next(request)


def _push_event(session_id: str, event: dict):
    q = _sse_queues.get(session_id)
    if q:
        try:
            q.put_nowait(json.dumps(event))
        except asyncio.QueueFull:
            pass


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("template_editor.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    csrf_token = _new_csrf_token()
    response = templates.TemplateResponse("login.html", {"request": request, "csrf_token": csrf_token})
    response.set_cookie(CSRF_COOKIE_NAME, csrf_token, secure=True, httponly=False, samesite="lax", path="/")
    return response


@app.post("/login")
async def login(password: str = Form(...)):
    if not hmac.compare_digest(password, APP_PASSWORD):
        return RedirectResponse(url="/login", status_code=302)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _make_session_cookie(),
        max_age=SESSION_TTL_SECONDS,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse(
        "about.html",
        {
            "request": request,
            "app_version": APP_VERSION,
            "build_time_utc": BUILD_TIME_UTC,
            "all_engines": ALL_ENGINES,
            "env_azure_endpoint": bool(os.getenv("AZURE_OCR_ENDPOINT", "").strip()),
            "env_azure_key": bool(os.getenv("AZURE_OCR_KEY", "").strip()),
            "env_ocrspace_key": bool(os.getenv("OCR_SPACE_API_KEY", "").strip()),
        },
    )


@app.get("/templates", response_class=HTMLResponse)
async def templates_list(request: Request):
    names = template_store.list_templates()
    tmpl_objects = []
    for name in names:
        t = template_store.get_template(name)
        if t:
            tmpl_objects.append(t)
    return templates.TemplateResponse(
        "templates_list.html",
        {
            "request": request,
            "templates": tmpl_objects,
        },
    )


@app.get("/templates/{template_name}/run", response_class=HTMLResponse)
async def template_run_page(request: Request, template_name: str):
    return templates.TemplateResponse(
        "template_run.html",
        {
            "request": request,
            "template_name": template_name,
        },
    )


def _banner_font_catalog() -> dict:
    """{family: [weights]} for the banner_run page dropdowns."""
    out: dict = {}
    for spec in BANNER_FONT_CATALOG:
        if not spec.exists():
            continue
        out.setdefault(spec.family, []).append(spec.weight)
    return out


@app.get("/banner", response_class=HTMLResponse)
async def banner_run_page(request: Request):
    """Local (CV-only) banner QA. Sibling of the OCR LLM flow at "/".

    Active when the "OCR local" toggle is clicked in the header.
    """
    return templates.TemplateResponse(
        "banner_run.html",
        {
            "request": request,
            "active_mode": "ocr_local",
            "font_catalog": _banner_font_catalog(),
            "threshold": BANNER_DEFAULT_THRESHOLD,
        },
    )


# Editor logging endpoints (called by JS, no-op beyond logging)
@app.post("/api/templates/_log_editor_save")
async def log_editor_save(request: Request):
    try:
        body = await request.json()
        log_event(
            "template_editor_saved",
            template_name=body.get("template_name", ""),
            zones_count=body.get("zones_count", 0),
        )
    except Exception:
        pass
    return JSONResponse({"ok": True})


@app.post("/api/templates/_log_editor_load")
async def log_editor_load(request: Request):
    try:
        body = await request.json()
        log_event("template_editor_load", template_name=body.get("template_name", ""))
    except Exception:
        pass
    return JSONResponse({"ok": True})


@app.get("/api/metrics/engine-usage/current_month")
async def api_engine_usage_current_month():
    return JSONResponse(get_current_month_usage())


@app.get("/api/metrics/llm-usage/current_month")
async def api_llm_usage_current_month():
    return JSONResponse(get_current_month_llm_usage())


@app.get("/image/{session_id}/{filename:path}")
async def get_image(session_id: str, filename: str):
    # Important for nested paths passed via URL encoding
    filename = unquote(filename)

    conn = get_db()
    row = conn.execute(
        "SELECT data FROM images WHERE session_id=? AND filename=?",
        (session_id, filename),
    ).fetchone()
    if not row:
        basename = filename.split("/")[-1]
        row = conn.execute(
            "SELECT data FROM images WHERE session_id=? AND filename=?",
            (session_id, basename),
        ).fetchone()
    conn.close()

    image_bytes: Optional[bytes] = None
    if row:
        image_bytes = bytes(row["data"])
    else:
        # Cloud Run cross-instance fallback: a different instance ran
        # `_process_session` and wrote the image to its local `/tmp` SQLite.
        # Pull the GCS mirror written by `put_session_image`.
        from app.pipeline.session_images import get_session_image
        image_bytes = get_session_image(session_id, filename)
        if image_bytes is None:
            return Response(status_code=404)

    fname_lower = filename.lower()
    if fname_lower.endswith(".png"):
        media_type = "image/png"
    elif fname_lower.endswith((".jpg", ".jpeg")):
        media_type = "image/jpeg"
    elif fname_lower.endswith(".gif"):
        media_type = "image/gif"
    elif fname_lower.endswith(".webp"):
        media_type = "image/webp"
    else:
        media_type = "application/octet-stream"
    return Response(content=image_bytes, media_type=media_type)


@app.get("/api/progress/{session_id}")
async def progress(session_id: str):
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_queues[session_id] = q

    conn = get_db()
    session_row = conn.execute(
        "SELECT status, total, pass_count, fail_count, manual_count, engines FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    conn.close()
    if session_row:
        status = str(session_row["status"] or "")
        total = int(session_row["total"] or 0)
        engines_raw = str(session_row["engines"] or "")
        engines = [e for e in engines_raw.split(",") if e]

        if status == "done":
            _push_event(session_id, {"event": "start", "total": total, "engines": engines})
            _push_event(
                session_id,
                {
                    "event": "done",
                    "pass": int(session_row["pass_count"] or 0),
                    "fail": int(session_row["fail_count"] or 0),
                    "manual": int(session_row["manual_count"] or 0),
                    "engines": engines,
                },
            )
        elif status == "error":
            _push_event(session_id, {"event": "error", "message": "Session failed"})
        else:
            _push_event(session_id, {"event": "start", "total": total, "engines": engines})

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                    yield f"data: {msg}\n\n"
                    data = json.loads(msg)
                    if data.get("event") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield 'data: {"event": "ping"}\n\n'
        finally:
            _sse_queues.pop(session_id, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _phase2_targets_from_manifest(manifest) -> list[Dict[str, object]]:
    targets_map: Dict[str, Dict[str, object]] = {}
    for t in manifest:
        for item in t.items:
            inner_path = item.archive_path.split("!/", 1)[-1]
            parts = [p for p in inner_path.split("/") if p]
            target_id = "/".join(parts[:-1]) if len(parts) > 1 else "default"
            entry = targets_map.setdefault(
                target_id,
                {
                    "target_id": target_id,
                    "en_available": False,
                    "preview_en_path": None,
                    "items_count": 0,
                },
            )
            entry["items_count"] = int(entry["items_count"]) + 1
            if item.lang == "en":
                entry["en_available"] = True
                if not entry["preview_en_path"]:
                    entry["preview_en_path"] = item.archive_path
    return [targets_map[k] for k in sorted(targets_map.keys())]


@app.post("/api/phase2/manifest")
async def phase2_manifest(
    zip_file: UploadFile = File(...),
    section_number: Optional[int] = Form(None),
    section_name: Optional[str] = Form(None),
):
    zip_bytes = await zip_file.read()
    upload_id = str(uuid.uuid4())
    manifest = build_zip_manifest(zip_bytes)

    conn = get_db()
    _cleanup_phase2_uploads(conn)
    conn.execute(
        "INSERT INTO phase2_uploads (upload_id, created_at, zip_bytes, section_number, section_name) VALUES (?,?,?,?,?)",
        (upload_id, time.time(), zip_bytes, section_number, section_name),
    )
    conn.commit()
    conn.close()

    targets = _phase2_targets_from_manifest(manifest)

    return JSONResponse({"upload_id": upload_id, "targets": targets})



@app.get("/api/phase2/preview/{upload_id}/{target_id:path}")
async def phase2_preview(upload_id: str, target_id: str):
    conn = get_db()
    row = conn.execute("SELECT zip_bytes, created_at FROM phase2_uploads WHERE upload_id=?", (upload_id,)).fetchone()
    conn.close()
    if not row:
        return JSONResponse({"error": "upload not found"}, status_code=404)
    if row["created_at"] < time.time() - SESSION_TTL_SECONDS:
        return JSONResponse({"error": "upload expired"}, status_code=410)

    manifest = build_zip_manifest(bytes(row["zip_bytes"]))
    targets = _phase2_targets_from_manifest(manifest)
    raw_target_id = target_id
    decoded_target_id = unquote(target_id)
    target = next((
        t for t in targets
        if str(t.get("target_id")) in {raw_target_id, decoded_target_id}
    ), None)
    if not target:
        return JSONResponse({"error": "target not found"}, status_code=404)
    en_path = target.get("preview_en_path")
    if not en_path:
        return JSONResponse({"error": "en not found"}, status_code=404)
    en_item = next((it for tt in manifest for it in tt.items if it.archive_path == en_path and it.lang == "en"), None)
    if not en_item:
        return JSONResponse({"error": "en not found"}, status_code=404)

    img_bytes = _read_archive_image(bytes(row["zip_bytes"]), en_item.archive_path)
    lower = en_item.archive_path.lower()
    media = "image/png" if lower.endswith(".png") else "image/jpeg"
    return Response(content=img_bytes, media_type=media)


@app.get("/api/phase2/error_paths/{session_id}")
async def phase2_error_paths(session_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT image_name FROM results WHERE session_id=? AND manual_decision='error' ORDER BY image_name",
        (session_id,),
    ).fetchall()
    conn.close()
    return JSONResponse({"paths": [r["image_name"] for r in rows]})


def _start_session_from_zip(
    zip_bytes: bytes,
    section_number: Optional[int],
    section_name: Optional[str],
    engines: List[str],
    target_zones: Dict[str, List[dict]],
) -> str:
    if not target_zones:
        raise ValueError("target_zones required — every session must be driven by a template's crop layout")
    session_id = str(uuid.uuid4())

    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (session_id, created_at, status, total, pass_count, fail_count, manual_count, engines) VALUES (?,?,?,?,?,?,?,?)",
        (session_id, time.time(), "pending", 0, 0, 0, 0, ",".join(engines)),
    )
    conn.commit()
    conn.close()

    asyncio.create_task(_process_session(session_id, zip_bytes, section_number, section_name, engines, target_zones))
    return session_id


def _prefetch_google_for_zip_items(
    queue_items: List[Any],
    zip_bytes: bytes,
    engines: List[str],
) -> tuple[Dict[int, bytes], Dict[int, CroppedImage], set[int], set[int], set[int], List[int]]:
    """
    Pre-read ZIP images and prefetch Google OCR in batch for ZIP/phase2 runs.

    Returns:
      - preloaded image bytes by queue index,
      - validated cropped image by queue index,
      - indices with image read failure,
      - indices with missing bbox,
      - indices where crop validation failed,
      - cache keys (id(image_bytes)) that were injected and must be cleared.

    Never raises.
    """
    preloaded_images: Dict[int, bytes] = {}
    cropped_images: Dict[int, CroppedImage] = {}
    read_failed_indices: set[int] = set()
    missing_bbox_indices: set[int] = set()
    crop_required_indices: set[int] = set()
    google_cache_ids: List[int] = []

    google_enabled = "google" in engines
    google_jobs: List[tuple[int, CroppedImage]] = []

    for idx, item in enumerate(queue_items):
        try:
            image_bytes = _read_archive_image(zip_bytes, item.archive_path)
        except Exception:
            read_failed_indices.add(idx)
            continue

        preloaded_images[idx] = image_bytes
        bbox = _resolve_real_bbox(item)
        if bbox is None:
            missing_bbox_indices.add(idx)
            continue
        try:
            cropped_images[idx] = _crop_zip_zone(image_bytes, bbox)
        except Exception:
            crop_required_indices.add(idx)
            continue
        if google_enabled:
            google_jobs.append((idx, cropped_images[idx]))

    if not google_enabled or not google_jobs:
        return (
            preloaded_images,
            cropped_images,
            read_failed_indices,
            missing_bbox_indices,
            crop_required_indices,
            google_cache_ids,
        )

    logger.info(
        "zip_google_batch_v2 items=%d chunks=%d",
        len(google_jobs),
        math.ceil(len(google_jobs) / 16),
    )

    inserted_ids: List[int] = []
    try:
        batch_results = google_batch_annotate_images([cropped.bytes for _, cropped in google_jobs])
        while len(batch_results) < len(google_jobs):
            batch_results.append(OCRResult("", 0.0, "google"))

        for (_, cropped), result in zip(google_jobs, batch_results):
            _google_cache_put(cropped.bytes, result)
            inserted_ids.append(id(cropped.bytes))

        google_cache_ids = inserted_ids
    except Exception:
        if inserted_ids:
            _google_cache_clear(inserted_ids)
        logger.warning("zip_google_batch_v2 prefetch_failed")

    return (
        preloaded_images,
        cropped_images,
        read_failed_indices,
        missing_bbox_indices,
        crop_required_indices,
        google_cache_ids,
    )


_VALID_ENGINES = set(ALL_ENGINES)


def _build_manual_detail(reason: str, translator_outlier, llm, lang: str) -> Optional[str]:
    """Render a human-readable tooltip for a MANUAL row.

    Called once per image in `_process_session`'s post-loop. Pulls the
    most informative diff for the chosen `reason`. Returns None when the
    reason has no extra detail to add.
    """
    if reason == "translator_outlier" and translator_outlier is not None:
        return translator_outlier.tooltip(lang)
    if reason == "engines_disagree":
        return "OCR engines disagreed on the text — none of the engine outputs matched each other after normalisation."
    if reason == "llm_rejected" and llm is not None and llm.real_differences:
        parts = []
        for d in llm.real_differences[:3]:
            kind = d.get("kind") or "diff"
            ocr_val = d.get("ocr") or "(empty)"
            ref_val = d.get("ref") or "(empty)"
            parts.append(f"{kind}: OCR '{ocr_val}' vs reference '{ref_val}'")
        prefix = "LLM flagged real differences: "
        suffix = ""
        if len(llm.real_differences) > 3:
            suffix = f" (+{len(llm.real_differences) - 3} more)"
        return prefix + "; ".join(parts) + suffix
    if reason == "localized_mismatch":
        return "Banner OCR doesn't match the reference text and the LLM judge couldn't classify the diff."
    return None


async def _process_session(
    session_id: str,
    zip_bytes: bytes,
    hint_number: Optional[int],
    hint_name: Optional[str],
    engines: List[str],
    target_zones: Dict[str, List[dict]],
):
    conn = get_db()
    conn.execute("UPDATE sessions SET status='processing' WHERE session_id=?", (session_id,))
    conn.commit()
    google_cache_ids: List[int] = []

    locked_section_number: Optional[int] = hint_number
    archive_label = f"session:{session_id}"

    try:
        log_event("run_start", run_id=session_id, archive_name=archive_label)

        contents = process_zip(zip_bytes)
        manifest = build_zip_manifest(zip_bytes, target_zones=target_zones)
        queue_items = [item for target in manifest for item in target.items]
        (
            preloaded_images,
            cropped_images,
            read_failed_indices,
            missing_bbox_indices,
            crop_required_indices,
            google_cache_ids,
        ) = _prefetch_google_for_zip_items(
            queue_items,
            zip_bytes,
            engines,
        )
        counters = _collect_zip_debug_counters(zip_bytes)
        counters["images_detected_total"] = len(queue_items)
        counters["images_queued_total"] = len(queue_items)
        counters["crop_success_total"] = len(cropped_images)
        counters["crop_failure_total"] = len(crop_required_indices)
        counters["ocr_dispatch_reached_total"] = 0
        counters["rows_inserted_by_reason"] = {}

        queue_with_bbox = sum(1 for item in queue_items if _resolve_real_bbox(item) is not None)
        queue_missing_bbox = max(0, len(queue_items) - queue_with_bbox)
        logger.info(
            "session_queue_stats session_id=%s queued=%d with_bbox=%d missing_bbox=%d read_failed=%d crop_success=%d crop_failed=%d",
            session_id,
            len(queue_items),
            queue_with_bbox,
            queue_missing_bbox,
            len(read_failed_indices),
            len(cropped_images),
            len(crop_required_indices),
        )

        total = len(queue_items)
        conn.execute("UPDATE sessions SET total=? WHERE session_id=?", (total, session_id))
        conn.commit()
        _push_event(session_id, {"event": "start", "total": total, "engines": engines})

        reference_lang = "en"
        reference_text_name = ""
        reference_sections = []
        reference_sections_by_lang: Dict[str, List[Any]] = {}
        selected_reference = None
        reference_selection_count = 0
        missing_en_reference = "en" not in contents.texts

        if not missing_en_reference:
            reference_text_name, reference_bytes = contents.texts["en"]
            reference_sections = await asyncio.get_event_loop().run_in_executor(
                None, extract_sections, reference_bytes, reference_text_name
            )
            reference_sections_by_lang["en"] = reference_sections

            if locked_section_number is None and hint_name:
                hint_lower = hint_name.strip().lower()
                for sec in reference_sections:
                    if hint_lower in sec.name.lower():
                        locked_section_number = sec.number
                        break

            if locked_section_number is not None:
                selected_reference = next((sec for sec in reference_sections if sec.number == locked_section_number), None)
                if selected_reference is not None:
                    reference_selection_count = 1

        en_item_indices = [i for i, manifest_item in enumerate(queue_items) if (manifest_item.lang or "").lower() == "en"]
        en_source_idx = en_item_indices[0] if en_item_indices else None
        en_source_image_name = queue_items[en_source_idx].archive_path if en_source_idx is not None else None
        en_source_ocr_results = None
        en_source_best_text = ""
        en_source_zone_indices = (
            [i for i in en_item_indices if queue_items[i].archive_path == en_source_image_name]
            if en_source_image_name else []
        )
        en_source_ocr_cache: dict = {}

        def _res_text(res: object) -> str:
            raw_text = res.get("text", "") if isinstance(res, dict) else getattr(res, "text", "")
            return raw_text if isinstance(raw_text, str) else ""

        def _res_conf(res: object) -> Optional[float]:
            raw_conf = res.get("confidence") if isinstance(res, dict) else getattr(res, "confidence", None)
            return float(raw_conf) if isinstance(raw_conf, (int, float)) else None

        def _pick_best_text(results: Dict[str, object]) -> tuple[Optional[str], str, float, bool]:
            """Adapter around `resolve_consensus`.

            Returns `(engine, text, confidence, engines_disagree)`. The
            4th tuple element is True when 2+ engines returned valid
            text but none of their consensus-normalised outputs matched,
            i.e. `rule_used == 'no_confidence_fallback'`. Callers use
            that flag to bypass the LLM judge and route straight to
            MANUAL — when OCR engines can't agree, asking an LLM to
            mediate adds cost without adding signal, and the operator
            rule is "false MANUAL > false PASS".
            """
            from app.pipeline.consensus import resolve_consensus
            from app.pipeline.ocr_dispatcher import ZoneEngineResult

            engine_results: list = []
            for eng, res in results.items():
                text = _res_text(res)
                if not text:
                    continue
                conf = _res_conf(res)
                engine_results.append(ZoneEngineResult(
                    engine=eng,
                    text=text,
                    confidence=conf,
                    latency_ms=0.0,
                    error=None,
                ))

            if not engine_results:
                return None, "", -1.0, False

            out = resolve_consensus(engine_results, engines_configured=True)
            selected_engine = out.get("selected_engine")
            selected_text = out.get("selected_text") or ""
            engines_disagree = (
                out.get("rule_used") == "no_confidence_fallback"
                and len(engine_results) >= 2
            )
            return selected_engine, selected_text, -1.0, engines_disagree

        if en_source_image_name is not None:
            try:
                en_source_bytes = preloaded_images.get(en_source_idx) if en_source_idx is not None else None
                if en_source_bytes is None:
                    en_source_bytes = _read_archive_image(zip_bytes, en_source_image_name)
                en_source_crop = cropped_images.get(en_source_idx) if en_source_idx is not None else None
                if en_source_crop is not None:
                    for zi in en_source_zone_indices:
                        zc = cropped_images.get(zi)
                        if zc is None:
                            continue
                        counters["ocr_dispatch_reached_total"] += 1
                        zocr = await asyncio.get_event_loop().run_in_executor(
                            None, _run_zone_ocr_for_engines, zc, engines,
                        )
                        en_source_ocr_cache[zi] = zocr
                    if en_source_ocr_cache:
                        en_source_ocr_results = en_source_ocr_cache.get(en_source_idx)
                        zone_texts = []
                        for zi in en_source_zone_indices:
                            if zi in en_source_ocr_cache:
                                _, zt, _, _ = _pick_best_text(en_source_ocr_cache[zi])
                                if zt:
                                    zone_texts.append(zt)
                        en_source_best_text = "\n".join(zone_texts)
                # Legacy whole-image fallback removed: the contract
                # requires a per-zone crop. With no template
                # (target_zones empty) we leave en_source_best_text
                # empty so the row finishes as MANUAL with reason
                # `missing_en_ocr_text` instead of silently OCRing
                # the entire banner image.
            except Exception:
                en_source_ocr_results = None
                en_source_best_text = ""

        missing_en_ocr_text = not bool(en_source_best_text)

        pass_count = fail_count = manual_count = 0
        # Collect per-image OCR and localized ref for post-loop aggregated comparison.
        image_ocr_accumulator: Dict[str, List[str]] = {}
        image_localized_ref: Dict[str, str] = {}
        # Raw (unnormalized) ref + lang per image — needed so the LLM judge
        # can see the actual texts (with diacritics, original punctuation)
        # rather than the strict-normalized form.
        image_localized_ref_raw: Dict[str, str] = {}
        image_lang: Dict[str, str] = {}
        # EN-anchor translator-outlier cache: image_name -> TranslatorOutlier|None.
        # Computed once per image when we first see the localised reference;
        # consumed in the post-loop to add `translator_outlier` info to MANUAL
        # rows regardless of whether the rule comparator flagged them.
        image_translator_outlier: Dict[str, Any] = {}
        # `True` if any zone of this image had `engines_disagree` (consensus
        # used `no_confidence_fallback`). Bypasses the LLM judge in post-loop
        # and routes straight to MANUAL — when OCR engines can't agree, the
        # selected text is unreliable and the LLM has nothing to mediate.
        image_engines_disagree: Dict[str, bool] = {}
        # Per-session LLM judge accumulator (for session_meta_json / UI).
        llm_calls_total = 0
        llm_cost_usd_total = 0.0
        llm_flipped_to_pass = 0

        for idx, item in enumerate(queue_items):
            lang = item.lang or "und"
            image_name = item.archive_path
            zone_name = (getattr(item, "zone_name", None) or "").strip()
            target_id = str(getattr(item, "target_id", "") or "")
            expected_by_lang = getattr(item, "expected_by_lang", {}) or {}

            image_bytes = preloaded_images.get(idx)
            if image_bytes is None and idx in read_failed_indices:
                counters["images_skipped_total"] += 1
                counters["images_skipped_by_reason"]["read_error"] = counters["images_skipped_by_reason"].get("read_error", 0) + 1
                manual_count += 1
                conn.execute(
                    """INSERT INTO results
                       (session_id, lang, image_name, text_name, ref_text,
                        section_name, section_number, status, score, reason,
                        ocr_results_json, best_engine, reference_confidence,
                        reference_score_top1, reference_score_top2, reference_margin, zone_name, target_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id,
                        lang,
                        image_name,
                        "",
                        "",
                        "",
                        None,
                        "MANUAL",
                        0.0,
                        "image_read_error",
                        "{}",
                        None,
                        0.0,
                        None,
                        None,
                        0.0,
                        zone_name,
                        target_id,
                    ),
                )
                conn.commit()
                counters["rows_inserted_by_reason"]["image_read_error"] = counters["rows_inserted_by_reason"].get("image_read_error", 0) + 1
                _push_event(
                    session_id,
                    {
                        "event": "item",
                        "idx": idx,
                        "lang": lang,
                        "image_name": image_name,
                        "status": "MANUAL",
                        "reason": "image_read_error",
                    },
                )
                continue
            if idx in missing_bbox_indices:
                counters["images_skipped_total"] += 1
                counters["images_skipped_by_reason"]["missing_bbox"] = counters["images_skipped_by_reason"].get("missing_bbox", 0) + 1
                manual_count += 1
                conn.execute(
                    """INSERT INTO results
                       (session_id, lang, image_name, text_name, ref_text,
                        section_name, section_number, status, score, reason,
                        ocr_results_json, best_engine, reference_confidence,
                        reference_score_top1, reference_score_top2, reference_margin, zone_name, target_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id,
                        lang,
                        image_name,
                        "",
                        "",
                        "",
                        None,
                        "MANUAL",
                        0.0,
                        "missing_bbox",
                        "{}",
                        None,
                        0.0,
                        None,
                        None,
                        0.0,
                        zone_name,
                        target_id,
                    ),
                )
                conn.commit()
                counters["rows_inserted_by_reason"]["missing_bbox"] = counters["rows_inserted_by_reason"].get("missing_bbox", 0) + 1
                _push_event(
                    session_id,
                    {
                        "event": "item",
                        "idx": idx,
                        "lang": lang,
                        "image_name": image_name,
                        "status": "MANUAL",
                        "reason": "missing_bbox",
                    },
                )
                continue
            if idx in crop_required_indices:
                counters["images_skipped_total"] += 1
                counters["images_skipped_by_reason"]["crop_required"] = counters["images_skipped_by_reason"].get("crop_required", 0) + 1
                manual_count += 1
                conn.execute(
                    """INSERT INTO results
                       (session_id, lang, image_name, text_name, ref_text,
                        section_name, section_number, status, score, reason,
                        ocr_results_json, best_engine, reference_confidence,
                        reference_score_top1, reference_score_top2, reference_margin, zone_name, target_id)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id,
                        lang,
                        image_name,
                        "",
                        "",
                        "",
                        None,
                        "MANUAL",
                        0.0,
                        "crop_required",
                        "{}",
                        None,
                        0.0,
                        None,
                        None,
                        0.0,
                        zone_name,
                        target_id,
                    ),
                )
                conn.commit()
                counters["rows_inserted_by_reason"]["crop_required"] = counters["rows_inserted_by_reason"].get("crop_required", 0) + 1
                _push_event(
                    session_id,
                    {
                        "event": "item",
                        "idx": idx,
                        "lang": lang,
                        "image_name": image_name,
                        "status": "MANUAL",
                        "reason": "crop_required",
                    },
                )
                continue

            if image_bytes is None:
                try:
                    image_bytes = _read_archive_image(zip_bytes, image_name)
                except Exception:
                    counters["images_skipped_total"] += 1
                    counters["images_skipped_by_reason"]["read_error"] = counters["images_skipped_by_reason"].get("read_error", 0) + 1
                    manual_count += 1
                    conn.execute(
                        """INSERT INTO results
                           (session_id, lang, image_name, text_name, ref_text,
                            section_name, section_number, status, score, reason,
                            ocr_results_json, best_engine, reference_confidence,
                            reference_score_top1, reference_score_top2, reference_margin, zone_name, target_id)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            session_id,
                            lang,
                            image_name,
                            "",
                            "",
                            "",
                            None,
                            "MANUAL",
                            0.0,
                            "image_read_error",
                            "{}",
                            None,
                            0.0,
                            None,
                            None,
                            0.0,
                            zone_name,
                            target_id,
                        ),
                    )
                    conn.commit()
                    counters["rows_inserted_by_reason"]["image_read_error"] = counters["rows_inserted_by_reason"].get("image_read_error", 0) + 1
                    _push_event(
                        session_id,
                        {
                            "event": "item",
                            "idx": idx,
                            "lang": lang,
                            "image_name": image_name,
                            "status": "MANUAL",
                            "reason": "image_read_error",
                        },
                    )
                    continue

            conn.execute(
                "INSERT OR REPLACE INTO images VALUES (?,?,?)",
                (session_id, image_name, image_bytes),
            )
            conn.commit()
            # Mirror to GCS so thumbnails render on Cloud Run regardless of
            # which instance serves the `/image/...` request. No-op when
            # SESSION_IMAGES_GCS_BUCKET is unset (dev / single-instance).
            try:
                from app.pipeline.session_images import put_session_image
                put_session_image(session_id, image_name, image_bytes)
            except Exception:
                logger.warning("session_image_mirror_skipped", exc_info=False)

            _push_event(
                session_id,
                {
                    "event": "progress",
                    "idx": idx,
                    "lang": lang,
                    "step": "ocr",
                    "message": f"OCR {lang} [{', '.join(engines)}]...",
                },
            )

            cropped_image = cropped_images.get(idx)
            if cropped_image is None:
                raise RuntimeError(f"Crop required but missing for {image_name}")

            if idx in en_source_ocr_cache:
                ocr_results = en_source_ocr_cache[idx]
            elif idx == en_source_idx and en_source_ocr_results is not None:
                ocr_results = en_source_ocr_results
            else:
                counters["ocr_dispatch_reached_total"] += 1
                ocr_results = await asyncio.get_event_loop().run_in_executor(
                    None,
                    _run_zone_ocr_for_engines,
                    cropped_image,
                    engines,
                )

            best_engine, best_text, best_conf, engines_disagree = _pick_best_text(ocr_results)
            if engines_disagree:
                image_engines_disagree[image_name] = True

            ocr_results_display = {eng: clean_for_display(_res_text(res)) for eng, res in ocr_results.items()}

            if best_text:
                image_ocr_accumulator.setdefault(image_name, []).append(best_text)

            en_ocr_text = en_source_best_text

            logger.info(
                "lang=%s best_engine=%s best_conf=%.3f ocr_len=%d en_ocr_len=%d",
                lang,
                best_engine,
                best_conf,
                len(best_text),
                len(en_ocr_text),
            )

            ref_text = ""
            row_lang_norm = (lang or "").strip().lower()
            display_lang = "en" if row_lang_norm.startswith("en") else (row_lang_norm or "und")
            if isinstance(expected_by_lang, dict) and zone_name:
                expected = expected_by_lang.get(display_lang) or expected_by_lang.get("en")
                if isinstance(expected, str):
                    ref_text = clean_for_display(expected)
            section_name_found = ""
            section_num_found = None
            status = "MANUAL"
            score_val = 0.0
            reason = "missing_en_reference_text" if missing_en_reference else "missing_en_ocr_text"
            text_name = reference_text_name
            reference_confidence = 0.0
            reference_score_top1 = None
            reference_score_top2 = None
            reference_margin = 0.0

            if not missing_en_reference:
                _push_event(
                    session_id,
                    {
                        "event": "progress",
                        "idx": idx,
                        "lang": lang,
                        "step": "match",
                        "message": f"Matching {lang}...",
                    },
                )

                if missing_en_ocr_text:
                    reason = "missing_en_ocr_text"
                else:
                    if selected_reference is None:
                        selection_once = await asyncio.get_event_loop().run_in_executor(
                            None,
                            select_best,
                            reference_sections,
                            en_ocr_text,
                            "en",
                            locked_section_number,
                            hint_name,
                        )
                        if selection_once.best:
                            selected_reference = selection_once.best.section
                            locked_section_number = selected_reference.number
                            reference_selection_count += 1

                    if selected_reference is not None:
                        selection = await asyncio.get_event_loop().run_in_executor(
                            None,
                            select_best,
                            [selected_reference],
                            en_ocr_text,
                            "en",
                            selected_reference.number,
                            hint_name,
                        )

                        status = selection.status
                        reason = selection.reason
                        reference_confidence = selection.reference_confidence
                        reference_score_top1 = selection.score_top1
                        reference_score_top2 = selection.score_top2
                        reference_margin = selection.confidence_margin
                        if selection.best:
                            section_name_found = selection.best.section.name
                            section_num_found = selection.best.section.number
                            score_val = selection.best.score
                    else:
                        reason = "no_reference_section"

                    if selected_reference is not None:
                        if display_lang not in reference_sections_by_lang:
                            text_entry = contents.texts.get(display_lang)
                            if text_entry is not None:
                                section_text_name, section_bytes = text_entry
                                section_list = await asyncio.get_event_loop().run_in_executor(
                                    None, extract_sections, section_bytes, section_text_name
                                )
                            else:
                                section_list = []
                            reference_sections_by_lang[display_lang] = section_list

                        localized_sections = reference_sections_by_lang.get(display_lang, [])

                        def _section_number(section_obj: Any) -> Optional[int]:
                            if isinstance(section_obj, dict):
                                value = section_obj.get("number")
                            else:
                                value = getattr(section_obj, "number", None)
                            return value if isinstance(value, int) else None

                        def _section_content(section_obj: Any) -> str:
                            if isinstance(section_obj, dict):
                                value = section_obj.get("content_text", "")
                            else:
                                value = getattr(section_obj, "content_text", "")
                            return value if isinstance(value, str) else ""

                        localized_reference = next(
                            (sec for sec in localized_sections if _section_number(sec) == selected_reference.number),
                            None,
                        )
                        ref_text = clean_for_display(_section_content(localized_reference)) if localized_reference is not None else ""

                        # Accumulate OCR texts and localized ref for post-loop aggregated comparison.
                        if image_name not in image_localized_ref:
                            loc_ref_raw = _section_content(localized_reference)
                            loc_ref_n = normalize_strict(loc_ref_raw)
                            if loc_ref_n:
                                image_localized_ref[image_name] = loc_ref_n
                                image_localized_ref_raw[image_name] = loc_ref_raw
                                image_lang[image_name] = lang or "und"

                                # EN-anchor: compare lang docx numbers against the
                                # EN reference section. Done once per image (the
                                # localised section text is identical across the
                                # zones of the same image).
                                if selected_reference is not None and lang != "en":
                                    from app.pipeline.translator_check import find_translator_outliers
                                    en_ref_raw = _section_content(selected_reference)
                                    image_translator_outlier[image_name] = find_translator_outliers(
                                        en_ref_raw, loc_ref_raw,
                                    )

                logger.info("lang=%s status=%s reason=%s section=%s", lang, status, reason, section_name_found)

            counters["images_processed_total"] += 1
            if status == "PASS":
                pass_count += 1
            else:
                manual_count += 1

            ocr_json_payload = {}
            for eng in engines:
                if eng not in ocr_results:
                    continue
                conf = _res_conf(ocr_results[eng])
                ocr_json_payload[eng] = {
                    "text": ocr_results_display.get(eng, ""),
                    "confidence": (round(conf, 4) if conf is not None else None),
                }

            ocr_json = json.dumps(ocr_json_payload)

            conn.execute(
                """INSERT INTO results
                   (session_id, lang, image_name, text_name, ref_text,
                    section_name, section_number, status, score, reason,
                    ocr_results_json, best_engine, reference_confidence,
                    reference_score_top1, reference_score_top2, reference_margin, zone_name, target_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    session_id,
                    lang,
                    image_name,
                    text_name,
                    ref_text,
                    section_name_found,
                    section_num_found,
                    status,
                    score_val,
                    reason,
                    ocr_json,
                    best_engine,
                    reference_confidence,
                    reference_score_top1,
                    reference_score_top2,
                    reference_margin,
                    zone_name,
                    target_id,
                ),
            )
            conn.commit()
            counters["rows_inserted_by_reason"][reason] = counters["rows_inserted_by_reason"].get(reason, 0) + 1

            _push_event(
                session_id,
                {
                    "event": "item",
                    "idx": idx,
                    "lang": lang,
                    "image_name": image_name,
                    "status": status,
                    "best_engine": best_engine,
                },
            )

        # Post-loop: re-evaluate each image status using aggregated OCR vs localized ref.
        # This correctly handles multi-zone images (same as frontend groupResultsBySourceImage).
        from app.normalizer import _levenshtein_similarity
        from app.pipeline.llm_adjudicator import adjudicate as _llm_adjudicate

        translator_outlier_count = 0
        # In-session LLM cache: hash(ocr+ref+lang) -> LLMVerdict.
        # Avoids re-billing for identical pairs in the same session.
        llm_cache: Dict[str, Any] = {}
        # Detailed per-image MANUAL reason for the UI tooltip — populated
        # alongside the row's `reason` code. Stored in `session_meta_json`
        # under `manual_reasons` so the frontend can show it on hover.
        manual_reason_details: Dict[str, str] = {}

        from app.normalizer import normalize as _normalize

        for img_name, ocr_texts in image_ocr_accumulator.items():
            ref_raw = image_localized_ref_raw.get(img_name)
            if not ref_raw:
                continue
            # Per-image lang governs the normalisation rules — most languages
            # use the default strict policy; French preserves spacing around
            # `!?:;»` and the guillemets `«»` (typography is mandatory in fr).
            lang_for_norm = image_lang.get(img_name, "und")
            ref_norm = _normalize(ref_raw, level="strict", lang=lang_for_norm)
            if not ref_norm:
                continue
            agg_raw = "\n".join(ocr_texts)
            agg_norm = _normalize(agg_raw, level="strict", lang=lang_for_norm)
            if not agg_norm:
                continue

            translator_outlier = image_translator_outlier.get(img_name)
            has_translator_outlier = (
                translator_outlier is not None and translator_outlier.has_mismatch
            )
            has_engines_disagree = image_engines_disagree.get(img_name, False)

            rule_pass = (agg_norm == ref_norm)

            llm_for_detail = None  # used by _build_manual_detail below

            if has_engines_disagree:
                # OCR engines couldn't agree on the text for at least one
                # zone of this image. The "best_text" the consensus picked
                # is unreliable — don't waste an LLM call mediating between
                # noisy OCR outputs. Straight to MANUAL.
                new_status = "MANUAL"
                new_reason = (
                    "translator_outlier" if has_translator_outlier else "engines_disagree"
                )
                if has_translator_outlier:
                    translator_outlier_count += 1
            elif rule_pass and not has_translator_outlier:
                # Clean PASS — banner and lang docx match, and lang docx
                # agrees with EN on all numeric facts.
                new_status = "PASS"
                new_reason = "strict_equal"
            elif rule_pass and has_translator_outlier:
                # Banner == lang docx, but lang docx disagrees with EN on a
                # numeric fact. Per the operator's rule "false MANUAL >
                # false PASS", flag this so the operator can review the
                # translator's text — even though the banner+docx pair is
                # internally consistent.
                new_status = "MANUAL"
                new_reason = "translator_outlier"
                translator_outlier_count += 1
            else:
                # Rule says MANUAL — give the LLM judge a shot at the gray
                # zone (only fires when similarity is in [SIM_MIN, SIM_MAX];
                # outside that window the rule verdict stands).
                sim = _levenshtein_similarity(agg_norm[:2000], ref_norm[:2000])
                ref_raw = image_localized_ref_raw.get(img_name, "")
                lang_for_llm = image_lang.get(img_name, "und")
                llm = _llm_adjudicate(
                    agg_raw, ref_raw, lang_for_llm, sim,
                    match_pass=False,
                    cache=llm_cache,
                )
                llm_for_detail = llm
                if llm.called and not llm.from_cache:
                    llm_calls_total += 1
                    if llm.cost_usd:
                        llm_cost_usd_total += float(llm.cost_usd)
                if llm.called and llm.verdict == "pass" and not has_translator_outlier:
                    # LLM says equivalent and translator agrees with EN.
                    new_status = "PASS"
                    new_reason = "llm_adjudicated"
                    llm_flipped_to_pass += 1
                elif llm.called and llm.verdict == "pass" and has_translator_outlier:
                    # LLM thinks banner ≈ lang docx semantically, but lang
                    # docx still has a translator numeric outlier vs EN.
                    # Surface the outlier for review.
                    new_status = "MANUAL"
                    new_reason = "translator_outlier"
                    translator_outlier_count += 1
                elif llm.called and llm.verdict == "fail":
                    new_status = "MANUAL"
                    new_reason = (
                        "translator_outlier" if has_translator_outlier else "llm_rejected"
                    )
                    if has_translator_outlier:
                        translator_outlier_count += 1
                else:
                    new_status = "MANUAL"
                    new_reason = (
                        "translator_outlier" if has_translator_outlier else "localized_mismatch"
                    )
                    if has_translator_outlier:
                        translator_outlier_count += 1

            # Build a human-readable detail string for the UI tooltip
            # whenever the row becomes MANUAL.
            if new_status == "MANUAL":
                detail = _build_manual_detail(
                    new_reason,
                    translator_outlier,
                    llm_for_detail,
                    image_lang.get(img_name, "und"),
                )
                if detail:
                    manual_reason_details[img_name] = detail

            conn.execute(
                """UPDATE results SET status=?, reason=?
                   WHERE session_id=? AND image_name=?
                   AND reason NOT IN ('image_read_error', 'missing_bbox', 'crop_required')""",
                (new_status, new_reason, session_id, img_name),
            )
        conn.commit()

        # Recount from DB after status updates.
        _counts = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status='PASS' THEN 1 ELSE 0 END) FROM results WHERE session_id=?",
            (session_id,),
        ).fetchone()
        pass_count = int(_counts[1] or 0)
        manual_count = int(_counts[0] or 0) - pass_count

        logger.info(
            "session_row_stats session_id=%s rows_by_reason=%s",
            session_id,
            json.dumps(counters["rows_inserted_by_reason"], ensure_ascii=False, sort_keys=True),
        )

        session_meta = {
            **counters,
            "reference_lang_locked": "en",
            "missing_en_reference": missing_en_reference,
            "missing_en_ocr_text": missing_en_ocr_text,
            "en_ocr_source_idx": en_source_idx,
            "en_ocr_source_image_name": en_source_image_name,
            "reference_selection_count": reference_selection_count,
            "reference_section_number": (selected_reference.number if selected_reference else None),
            "reference_section_name": (selected_reference.name if selected_reference else None),
            "reference_text_lang": reference_lang,
            "reference_text_name": reference_text_name,
            # LLM judge stats for this session (consumed by the UI banner).
            "llm_calls_total": llm_calls_total,
            "llm_cost_usd_total": round(llm_cost_usd_total, 6),
            "llm_flipped_to_pass": llm_flipped_to_pass,
            # EN-anchor stats — translator outliers caught by the
            # deterministic numeric-fact check (no LLM cost involved).
            "translator_outlier_count": translator_outlier_count,
            # Engines disagreement count — images where the consensus
            # fell back to no_confidence_fallback on at least one zone.
            "engines_disagree_count": sum(1 for v in image_engines_disagree.values() if v),
            # Per-image tooltip text for MANUAL rows. Keyed by archive_path.
            # The frontend renders these on hover over the status badge.
            "manual_reasons": manual_reason_details,
        }

        conn.execute(
            """UPDATE sessions SET status='done',
               pass_count=?, fail_count=?, manual_count=?, session_meta_json=?
               WHERE session_id=?""",
            (pass_count, 0, manual_count, json.dumps(session_meta, ensure_ascii=False), session_id),
        )
        conn.commit()
        _push_event(
            session_id,
            {
                "event": "done",
                "pass": pass_count,
                "fail": 0,
                "manual": manual_count,
                "engines": engines,
                "meta": session_meta,
            },
        )
        log_event("run_zip_counters", run_id=session_id, **session_meta)
        log_event("run_end", run_id=session_id, status="ok")

    except Exception as exc:
        logger.exception("Processing error for session %s", session_id)
        conn.execute("UPDATE sessions SET status='error' WHERE session_id=?", (session_id,))
        conn.commit()
        _push_event(session_id, {"event": "error", "message": str(exc)})
        log_event("run_end", run_id=session_id, status="error")
    finally:
        if google_cache_ids:
            _google_cache_clear(google_cache_ids)
        conn.close()


@app.get("/api/results/{session_id}")
async def get_results(
    session_id: str,
    page: int = 1,
    hide_pass: bool = False,
    per_page: int = 20,
):
    conn = get_db()
    session = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if not session:
        conn.close()
        return JSONResponse({"error": "session not found"}, status_code=404)

    base_q = "SELECT * FROM results WHERE session_id=?" + (" AND status != 'PASS'" if hide_pass else "")
    total_rows = conn.execute(
        "SELECT COUNT(*) FROM results WHERE session_id=?" + (" AND status != 'PASS'" if hide_pass else ""),
        [session_id],
    ).fetchone()[0]

    rows = conn.execute(
        base_q + " ORDER BY id LIMIT ? OFFSET ?",
        [session_id, per_page, (page - 1) * per_page],
    ).fetchall()
    conn.close()

    results = []
    for r in rows:
        ocr_data = {}
        try:
            raw_json = r["ocr_results_json"]
            if raw_json:
                ocr_data = json.loads(raw_json)
        except Exception:
            pass
        results.append(
            {
                "id": r["id"],
                "lang": r["lang"],
                "image_name": r["image_name"],
                "text_name": r["text_name"],
                "ref_text": r["ref_text"],
                "section_name": r["section_name"],
                "section_number": r["section_number"],
                "status": r["status"],
                "score": r["score"],
                "reason": r["reason"],
                "manual_decision": r["manual_decision"],
                "zone_name": r["zone_name"] if "zone_name" in r.keys() else "",
                "target_id": r["target_id"] if "target_id" in r.keys() else "",
                "ocr_results": ocr_data,
                "best_engine": r["best_engine"],
                "reference_confidence": r["reference_confidence"] if "reference_confidence" in r.keys() else None,
                "reference": {
                    "confidence": r["reference_confidence"] if "reference_confidence" in r.keys() else None,
                    "score_top1": r["reference_score_top1"] if "reference_score_top1" in r.keys() else None,
                    "score_top2": r["reference_score_top2"] if "reference_score_top2" in r.keys() else None,
                    "margin": r["reference_margin"] if "reference_margin" in r.keys() else None,
                },
            }
        )

    try:
        engines_str = session["engines"] or "google"
        engines_list = [e for e in engines_str.split(",") if e]
    except Exception:
        engines_list = ["google"]

    # Overall confidence must include all zones in the image/session,
    # not only the current paginated slice.
    total_count, total_sum = 0, 0.0
    conn2 = get_db()
    try:
        total_count, total_sum = conn2.execute(
            "SELECT COUNT(*), COALESCE(SUM(COALESCE(reference_confidence, 0.0)), 0.0) FROM results WHERE session_id=?",
            (session_id,),
        ).fetchone()
    finally:
        conn2.close()
    overall_reference_confidence = (float(total_sum) / float(total_count)) if total_count else 0.0

    session_meta = {}
    try:
        raw_meta = session["session_meta_json"] if "session_meta_json" in session.keys() else None
        if raw_meta:
            session_meta = json.loads(raw_meta)
    except Exception:
        session_meta = {}

    return JSONResponse(
        {
            "session": {
                "session_id": session["session_id"],
                "status": session["status"],
                "total": session["total"],
                "pass_count": session["pass_count"],
                "fail_count": session["fail_count"],
                "manual_count": session["manual_count"],
                "engines": engines_list,
                "overall_reference_confidence": round(overall_reference_confidence, 4),
                "meta": session_meta,
            },
            "results": results,
            "page": page,
            "per_page": per_page,
            "total_rows": total_rows,
            "total_pages": max(1, (total_rows + per_page - 1) // per_page),
        }
    )


@app.post("/api/decide/{result_id}")
async def decide(result_id: int, decision: str = Form(...)):
    if decision not in ("ok", "error"):
        return JSONResponse({"error": "invalid decision"}, status_code=400)
    conn = get_db()
    row = conn.execute("SELECT session_id FROM results WHERE id=?", (result_id,)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"error": "result not found"}, status_code=404)
    conn.execute("UPDATE results SET manual_decision=? WHERE id=?", (decision, result_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "session_id": row["session_id"]})


@app.post("/api/debug/ocr")
async def debug_ocr(zip_file: UploadFile = File(...)):
    zip_bytes = await zip_file.read()
    contents = process_zip(zip_bytes)
    manifest = build_zip_manifest(zip_bytes)
    manifest_items = [item for target in manifest for item in target.items]
    langs = sorted(contents.images.keys())
    if not langs:
        return JSONResponse({"error": "no images found"})
    results = []
    for lang in langs[:2]:
        image_name = contents.image_names[lang]
        image_bytes = contents.images[lang]
        manifest_item = _select_manifest_item_for_debug(manifest_items, lang, image_name)
        bbox = _resolve_real_bbox(manifest_item) if manifest_item is not None else None
        reason = None
        ocr_results = {}
        if bbox is None:
            reason = "missing_bbox"
        else:
            try:
                cropped_image = _crop_zip_zone(image_bytes, bbox)
                ocr_results = await asyncio.get_event_loop().run_in_executor(
                    None,
                    _run_zone_ocr_for_engines,
                    cropped_image,
                    ALL_ENGINES,
                )
            except Exception:
                reason = "crop_required"

        best = max(
            ocr_results.values() if ocr_results else [],
            key=lambda r: (float(r.confidence) if isinstance(r.confidence, (int, float)) else -1.0),
            default=None,
        )
        best_text = best.text if best else ""
        sections_data, ref_info = [], {}
        if lang in contents.texts:
            fname, file_bytes = contents.texts[lang]
            sections = await asyncio.get_event_loop().run_in_executor(None, extract_sections, file_bytes, fname)
            selection = await asyncio.get_event_loop().run_in_executor(None, select_best, sections, best_text, lang, None, None)
            for s in sections:
                sections_data.append(
                    {
                        "number": s.number,
                        "name": s.name,
                        "content_preview": s.content_text[:80],
                        "norm_strict": normalize_strict(s.content_text),
                    }
                )
            if selection.best:
                ref_info = {
                    "matched_section": selection.best.section.name,
                    "status": selection.status,
                    "reason": selection.reason,
                    "strict_equal": selection.best.strict_equal,
                }
        results.append(
            {
                "lang": lang,
                "image_name": image_name,
                "reason": reason,
                "ocr_engines": {eng: {"text": r.text[:200], "confidence": r.confidence} for eng, r in ocr_results.items()},
                "sections": sections_data,
                "match": ref_info,
            }
        )
    return JSONResponse(results)


@app.get("/api/download/{session_id}")
async def download_errors(session_id: str):
    conn = get_db()
    rows = conn.execute(
        """SELECT r.image_name, i.data
           FROM results r
           JOIN images i ON r.session_id=i.session_id AND r.image_name=i.filename
           WHERE r.session_id=? AND r.manual_decision='error'""",
        (session_id,),
    ).fetchall()
    conn.close()
    if not rows:
        return JSONResponse({"error": "no error images"}, status_code=404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            zf.writestr(row["image_name"], bytes(row["data"]))
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=errors_{session_id[:8]}.zip"},
    )
