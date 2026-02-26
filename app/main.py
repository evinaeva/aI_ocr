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
import os
import sqlite3
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .normalizer import normalize_strict, clean_for_display
from .ocr import run_ocr_multi, ALL_ENGINES
from .section_matcher import extract_sections, select_best
from .zip_processor import process_zip
from .version import APP_VERSION, BUILD_TIME_UTC, get_build_info
from .logging_utils import log_event
from .pipeline.template_routes import router as template_router
from .pipeline.template_editor_routes import editor_router
from .pipeline.preview_routes import preview_router
from .pipeline.run_routes import run_router
from .pipeline.history_routes import history_router  # always imported — see Phase 6 fix
from .pipeline import template_store

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
    conn.executescript("""
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
    """)
    # Migration: add engines column if missing (for old deployments)
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN engines TEXT")
        conn.commit()
    except Exception:
        pass
    # Migration: add new columns to results if missing
    for col in ["ocr_results_json TEXT", "best_engine TEXT"]:
        try:
            conn.execute(f"ALTER TABLE results ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log_event("app_start", app_version=APP_VERSION)
    yield


app = FastAPI(lifespan=lifespan, title="OCR Localization Checker")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(template_router)
app.include_router(editor_router)
app.include_router(preview_router)
app.include_router(run_router)
app.include_router(history_router)  # unconditional — endpoints return 503 when persistence disabled
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_sse_queues: Dict[str, asyncio.Queue] = {}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign_payload(payload_b64: str) -> str:
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
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

    if method == "POST" and not is_api:
        content_type = (request.headers.get("content-type") or "").lower()
        is_form_post = (
            content_type.startswith("application/x-www-form-urlencoded")
            or content_type.startswith("multipart/form-data")
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
    return templates.TemplateResponse("index.html", {"request": request})


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
    return templates.TemplateResponse("about.html", {
        "request": request,
        "app_version": APP_VERSION,
        "build_time_utc": BUILD_TIME_UTC,
        "all_engines": ALL_ENGINES,
        "env_azure_endpoint": bool(os.getenv("AZURE_OCR_ENDPOINT", "").strip()),
        "env_azure_key": bool(os.getenv("AZURE_OCR_KEY", "").strip()),
        "env_ocrspace_key": bool(os.getenv("OCR_SPACE_API_KEY", "").strip()),
    })


@app.get("/templates", response_class=HTMLResponse)
async def templates_list(request: Request):
    names = template_store.list_templates()
    tmpl_objects = []
    for name in names:
        t = template_store.get_template(name)
        if t:
            tmpl_objects.append(t)
    return templates.TemplateResponse("templates_list.html", {
        "request": request,
        "templates": tmpl_objects,
    })


@app.get("/templates/{template_name}/run", response_class=HTMLResponse)
async def template_run_page(request: Request, template_name: str):
    return templates.TemplateResponse("template_run.html", {
        "request": request,
        "template_name": template_name,
    })


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


@app.get("/image/{session_id}/{filename:path}")
async def get_image(session_id: str, filename: str):
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
    if not row:
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
    return Response(content=bytes(row["data"]), media_type=media_type)


@app.get("/api/progress/{session_id}")
async def progress(session_id: str):
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_queues[session_id] = q

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


_VALID_ENGINES = set(ALL_ENGINES)


@app.post("/api/upload")
async def upload(
    zip_file: UploadFile = File(...),
    engines: Optional[str] = Form("google"),  # comma-separated list
    section_number: Optional[int] = Form(None),
    section_name: Optional[str] = Form(None),
):
    selected = [e.strip() for e in (engines or "google").split(",") if e.strip() in _VALID_ENGINES]
    if not selected:
        selected = ["google"]

    session_id = str(uuid.uuid4())
    zip_bytes = await zip_file.read()

    conn = get_db()
    conn.execute(
        "INSERT INTO sessions (session_id, created_at, status, total, pass_count, fail_count, manual_count, engines) VALUES (?,?,?,?,?,?,?,?)",
        (session_id, time.time(), "pending", 0, 0, 0, 0, ",".join(selected)),
    )
    conn.commit()
    conn.close()

    asyncio.create_task(
        _process_session(session_id, zip_bytes, section_number, section_name, selected)
    )

    return JSONResponse({"session_id": session_id, "engines": selected})


async def _process_session(
    session_id: str,
    zip_bytes: bytes,
    hint_number: Optional[int],
    hint_name: Optional[str],
    engines: List[str],
):
    conn = get_db()
    conn.execute(
        "UPDATE sessions SET status='processing' WHERE session_id=?", (session_id,)
    )
    conn.commit()

    locked_section_number: Optional[int] = hint_number

    archive_label = f"session:{session_id}"

    try:
        log_event("run_start", run_id=session_id, archive_name=archive_label)

        contents = process_zip(zip_bytes)
        langs = sorted(contents.images.keys())
        total = len(langs)

        if hint_name and locked_section_number is None:
            ref_lang = "en" if "en" in contents.texts else (
                sorted(contents.texts.keys())[0] if contents.texts else None
            )
            if ref_lang:
                ref_fname, ref_bytes = contents.texts[ref_lang]
                ref_sections = await asyncio.get_event_loop().run_in_executor(
                    None, extract_sections, ref_bytes, ref_fname
                )
                hint_lower = hint_name.strip().lower()
                for sec in ref_sections:
                    if hint_lower in sec.name.lower():
                        locked_section_number = sec.number
                        break

        conn.execute(
            "UPDATE sessions SET total=? WHERE session_id=?", (total, session_id)
        )
        conn.commit()
        _push_event(session_id, {"event": "start", "total": total, "engines": engines})

        pass_count = fail_count = manual_count = 0

        for idx, lang in enumerate(langs):
            image_bytes = contents.images[lang]
            image_name  = contents.image_names[lang]
            image_key   = image_name.split("/")[-1]

            conn.execute(
                "INSERT OR REPLACE INTO images VALUES (?,?,?)",
                (session_id, image_key, image_bytes),
            )
            conn.commit()

            _push_event(session_id, {
                "event": "progress", "idx": idx, "lang": lang,
                "step": "ocr", "message": f"OCR {lang} [{', '.join(engines)}]..."
            })

            ocr_results = await asyncio.get_event_loop().run_in_executor(
                None, run_ocr_multi, image_bytes, engines
            )

            best_engine = None
            best_text   = ""
            best_conf   = -1.0
            for eng, res in ocr_results.items():
                if res.confidence > best_conf and res.text:
                    best_conf   = res.confidence
                    best_text   = res.text
                    best_engine = eng

            ocr_results_display = {
                eng: clean_for_display(res.text)
                for eng, res in ocr_results.items()
            }

            logger.info("lang=%s best_engine=%s best_conf=%.3f ocr_len=%d",
                        lang, best_engine, best_conf, len(best_text))

            ref_text = ""
            section_name_found = ""
            section_num_found  = None
            status     = "MANUAL"
            score_val  = 0.0
            reason     = "no_text_file"
            text_name  = ""

            if lang in contents.texts:
                fname, file_bytes = contents.texts[lang]
                text_name = fname

                _push_event(session_id, {
                    "event": "progress", "idx": idx, "lang": lang,
                    "step": "match", "message": f"Matching {lang}..."
                })

                sections = await asyncio.get_event_loop().run_in_executor(
                    None, extract_sections, file_bytes, fname
                )

                selection = await asyncio.get_event_loop().run_in_executor(
                    None, select_best, sections, best_text, lang,
                    locked_section_number, hint_name
                )

                status = selection.status
                reason = selection.reason
                if selection.best:
                    ref_text           = clean_for_display(selection.best.section.content_text)
                    section_name_found = selection.best.section.name
                    section_num_found  = selection.best.section.number
                    score_val          = selection.best.score

                    if locked_section_number is None and section_num_found is not None:
                        locked_section_number = section_num_found

                logger.info(
                    "lang=%s status=%s reason=%s section=%s",
                    lang, status, reason, section_name_found,
                )
            else:
                status = "MANUAL"
                reason = "no_text_file"

            if status == "PASS":
                pass_count += 1
            elif status == "FAIL":
                fail_count += 1
            else:
                manual_count += 1

            ocr_json = json.dumps({
                eng: {
                    "text": ocr_results_display.get(eng, ""),
                    "confidence": round(ocr_results[eng].confidence, 4),
                }
                for eng in engines
                if eng in ocr_results
            })

            conn.execute(
                """INSERT INTO results
                   (session_id, lang, image_name, text_name, ref_text,
                    section_name, section_number, status, score, reason,
                    ocr_results_json, best_engine)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, lang, image_key, text_name, ref_text,
                 section_name_found, section_num_found, status, score_val, reason,
                 ocr_json, best_engine),
            )
            conn.commit()

            _push_event(session_id, {
                "event": "item", "idx": idx, "lang": lang,
                "image_name": image_key, "status": status,
                "best_engine": best_engine,
            })

        conn.execute(
            """UPDATE sessions SET status='done',
               pass_count=?, fail_count=?, manual_count=?
               WHERE session_id=?""",
            (pass_count, fail_count, manual_count, session_id),
        )
        conn.commit()
        _push_event(session_id, {
            "event": "done",
            "pass": pass_count, "fail": fail_count, "manual": manual_count,
            "engines": engines,
        })
        log_event("run_end", run_id=session_id, status="ok")

    except Exception as exc:
        logger.exception("Processing error for session %s", session_id)
        conn.execute(
            "UPDATE sessions SET status='error' WHERE session_id=?", (session_id,)
        )
        conn.commit()
        _push_event(session_id, {"event": "error", "message": str(exc)})
        log_event("run_end", run_id=session_id, status="error")
    finally:
        conn.close()


@app.get("/api/results/{session_id}")
async def get_results(
    session_id: str,
    page: int = 1,
    hide_pass: bool = False,
    per_page: int = 20,
):
    conn = get_db()
    session = conn.execute(
        "SELECT * FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
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
        results.append({
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
            "ocr_results": ocr_data,
            "best_engine": r["best_engine"],
        })

    try:
        engines_str = session["engines"] or "google"
        engines_list = [e for e in engines_str.split(",") if e]
    except Exception:
        engines_list = ["google"]

    return JSONResponse({
        "session": {
            "session_id": session["session_id"],
            "status": session["status"],
            "total": session["total"],
            "pass_count": session["pass_count"],
            "fail_count": session["fail_count"],
            "manual_count": session["manual_count"],
            "engines": engines_list,
        },
        "results": results,
        "page": page,
        "per_page": per_page,
        "total_rows": total_rows,
        "total_pages": max(1, (total_rows + per_page - 1) // per_page),
    })


@app.post("/api/decide/{result_id}")
async def decide(result_id: int, decision: str = Form(...)):
    if decision not in ("ok", "error"):
        return JSONResponse({"error": "invalid decision"}, status_code=400)
    conn = get_db()
    conn.execute("UPDATE results SET manual_decision=? WHERE id=?", (decision, result_id))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/api/debug/ocr")
async def debug_ocr(zip_file: UploadFile = File(...)):
    zip_bytes = await zip_file.read()
    contents = process_zip(zip_bytes)
    langs = sorted(contents.images.keys())
    if not langs:
        return JSONResponse({"error": "no images found"})
    results = []
    for lang in langs[:2]:
        image_bytes = contents.images[lang]
        ocr_results = await asyncio.get_event_loop().run_in_executor(
            None, run_ocr_multi, image_bytes, ALL_ENGINES
        )
        best = max(ocr_results.values(), key=lambda r: r.confidence, default=None)
        best_text = best.text if best else ""
        sections_data, ref_info = [], {}
        if lang in contents.texts:
            fname, file_bytes = contents.texts[lang]
            sections = await asyncio.get_event_loop().run_in_executor(
                None, extract_sections, file_bytes, fname
            )
            selection = await asyncio.get_event_loop().run_in_executor(
                None, select_best, sections, best_text, lang, None, None
            )
            for s in sections:
                sections_data.append({
                    "number": s.number, "name": s.name,
                    "content_preview": s.content_text[:80],
                    "norm_strict": normalize_strict(s.content_text),
                })
            if selection.best:
                ref_info = {
                    "matched_section": selection.best.section.name,
                    "status": selection.status,
                    "reason": selection.reason,
                    "strict_equal": selection.best.strict_equal,
                }
        results.append({
            "lang": lang,
            "image_name": contents.image_names[lang],
            "ocr_engines": {
                eng: {"text": r.text[:200], "confidence": r.confidence}
                for eng, r in ocr_results.items()
            },
            "sections": sections_data,
            "match": ref_info,
        })
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
