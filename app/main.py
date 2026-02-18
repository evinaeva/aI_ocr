"""
OCR Localization Checker — FastAPI main application.
"""
import asyncio
import io
import json
import logging
import os
import sqlite3
import tempfile
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .normalizer import normalize_strict, normalize_soft, clean_for_display
from .ocr import run_ocr
from .section_matcher import extract_sections, select_best
from .zip_processor import process_zip

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

DB_PATH = os.getenv("DB_PATH", "/tmp/sessions.db")

# ─── DB helpers ──────────────────────────────────────────────────────────────

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
            manual_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            lang TEXT,
            image_name TEXT,
            text_name TEXT,
            ocr_text TEXT,
            ref_text TEXT,
            section_name TEXT,
            section_number INTEGER,
            status TEXT,
            score REAL,
            reason TEXT,
            manual_decision TEXT,
            ocr_engine TEXT,
            ocr_confidence REAL
        );
        CREATE TABLE IF NOT EXISTS images (
            session_id TEXT,
            filename TEXT,
            data BLOB,
            PRIMARY KEY (session_id, filename)
        );
    """)
    conn.commit()
    conn.close()


# ─── Lifespan ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan, title="OCR Localization Checker")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_sse_queues: Dict[str, asyncio.Queue] = {}


def _push_event(session_id: str, event: dict):
    q = _sse_queues.get(session_id)
    if q:
        try:
            q.put_nowait(json.dumps(event))
        except asyncio.QueueFull:
            pass


# ─── Routes: UI ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ─── Routes: Image proxy ─────────────────────────────────────────────────────

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


# ─── Routes: SSE progress ────────────────────────────────────────────────────

@app.get("/progress/{session_id}")
async def progress(session_id: str):
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
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
                    yield "data: {\"event\": \"ping\"}\n\n"
        finally:
            _sse_queues.pop(session_id, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ─── Routes: Upload & process ────────────────────────────────────────────────

_VALID_ENGINES = {"google", "azure", "ocrspace"}

@app.post("/upload")
async def upload(
    zip_file: UploadFile = File(...),
    engine: Optional[str] = Form("google"),
    section_number: Optional[int] = Form(None),
    section_name: Optional[str] = Form(None),
):
    if engine not in _VALID_ENGINES:
        engine = "google"

    session_id = str(uuid.uuid4())
    zip_bytes = await zip_file.read()

    conn = get_db()
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        (session_id, time.time(), "pending", 0, 0, 0, 0),
    )
    conn.commit()
    conn.close()

    asyncio.create_task(
        _process_session(session_id, zip_bytes, section_number, section_name, engine)
    )

    return JSONResponse({"session_id": session_id})


async def _process_session(
    session_id: str,
    zip_bytes: bytes,
    hint_number: Optional[int],
    hint_name: Optional[str],
    engine: str = "google",
):
    conn = get_db()
    conn.execute(
        "UPDATE sessions SET status='processing' WHERE session_id=?", (session_id,)
    )
    conn.commit()

    locked_section_number: Optional[int] = hint_number

    try:
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
                        logger.info(
                            "Resolved hint_name=%r -> section #%d (%s) from lang=%s",
                            hint_name, locked_section_number, sec.name, ref_lang
                        )
                        break

        conn.execute(
            "UPDATE sessions SET total=? WHERE session_id=?", (total, session_id)
        )
        conn.commit()

        _push_event(session_id, {"event": "start", "total": total})

        pass_count = fail_count = manual_count = 0

        for idx, lang in enumerate(langs):
            image_bytes = contents.images[lang]
            image_name  = contents.image_names[lang]
            image_key = image_name.split("/")[-1]

            conn.execute(
                "INSERT OR REPLACE INTO images VALUES (?,?,?)",
                (session_id, image_key, image_bytes),
            )
            conn.commit()

            _push_event(session_id, {
                "event": "progress", "idx": idx, "lang": lang,
                "step": "ocr", "message": f"OCR {lang} [{engine}]..."
            })
            ocr_result = await asyncio.get_event_loop().run_in_executor(
                None, run_ocr, image_bytes, engine
            )
            ocr_text_raw = ocr_result.text
            ocr_text_display = clean_for_display(ocr_text_raw)
            logger.info("lang=%s image=%s ocr_len=%d conf=%.3f engine=%s",
                        lang, image_key, len(ocr_text_raw), ocr_result.confidence, ocr_result.engine)

            text_name = ""
            ref_text = ""
            section_name_found = ""
            section_num_found = None
            status = "MANUAL"
            score_val = 0.0
            reason = "no_text_file"

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
                    None, select_best, sections, ocr_text_raw, lang, locked_section_number, hint_name
                )

                status = selection.status
                reason = selection.reason
                if selection.best:
                    ref_text = clean_for_display(selection.best.section.content_text)
                    section_name_found = selection.best.section.name
                    section_num_found = selection.best.section.number
                    score_val = selection.best.score

                    if locked_section_number is None and section_num_found is not None:
                        locked_section_number = section_num_found
                        logger.info("Locked section #%d (%s) from lang=%s",
                                    locked_section_number, section_name_found, lang)

                logger.info(
                    "lang=%s status=%s reason=%s section=%s locked_num=%s "
                    "ocr_norm='%.60s' ref_norm='%.60s'",
                    lang, status, reason, section_name_found,
                    locked_section_number,
                    normalize_strict(ocr_text_raw),
                    normalize_strict(ref_text),
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

            conn.execute(
                """INSERT INTO results
                   (session_id, lang, image_name, text_name, ocr_text, ref_text,
                    section_name, section_number, status, score, reason,
                    ocr_engine, ocr_confidence)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, lang, image_key, text_name, ocr_text_display, ref_text,
                 section_name_found, section_num_found, status, score_val, reason,
                 ocr_result.engine, ocr_result.confidence),
            )
            conn.commit()

            _push_event(session_id, {
                "event": "item", "idx": idx, "lang": lang,
                "image_name": image_key, "status": status,
                "engine": ocr_result.engine,
                "confidence": round(ocr_result.confidence, 3),
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
        })

    except Exception as exc:
        logger.exception("Processing error for session %s", session_id)
        conn.execute(
            "UPDATE sessions SET status='error' WHERE session_id=?", (session_id,)
        )
        conn.commit()
        _push_event(session_id, {"event": "error", "message": str(exc)})
    finally:
        conn.close()


# ─── Routes: Results ─────────────────────────────────────────────────────────

@app.get("/results/{session_id}")
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

    query = "SELECT * FROM results WHERE session_id=?"
    params: list = [session_id]
    if hide_pass:
        query += " AND status != 'PASS'"

    total_rows = conn.execute(
        f"SELECT COUNT(*) FROM results WHERE session_id=?" +
        (" AND status != 'PASS'" if hide_pass else ""),
        [session_id],
    ).fetchone()[0]

    query += " ORDER BY id LIMIT ? OFFSET ?"
    params += [per_page, (page - 1) * per_page]

    rows = conn.execute(query, params).fetchall()
    conn.close()

    results = []
    for r in rows:
        results.append({
            "id": r["id"],
            "lang": r["lang"],
            "image_name": r["image_name"],
            "text_name": r["text_name"],
            "ocr_text": r["ocr_text"],
            "ref_text": r["ref_text"],
            "section_name": r["section_name"],
            "section_number": r["section_number"],
            "status": r["status"],
            "score": r["score"],
            "reason": r["reason"],
            "manual_decision": r["manual_decision"],
            "ocr_engine": r["ocr_engine"] if "ocr_engine" in r.keys() else None,
            "ocr_confidence": r["ocr_confidence"] if "ocr_confidence" in r.keys() else None,
        })

    return JSONResponse({
        "session": {
            "session_id": session["session_id"],
            "status": session["status"],
            "total": session["total"],
            "pass_count": session["pass_count"],
            "fail_count": session["fail_count"],
            "manual_count": session["manual_count"],
        },
        "results": results,
        "page": page,
        "per_page": per_page,
        "total_rows": total_rows,
        "total_pages": max(1, (total_rows + per_page - 1) // per_page),
    })


# ─── Routes: Manual review decision ─────────────────────────────────────────

@app.post("/decide/{result_id}")
async def decide(result_id: int, decision: str = Form(...)):
    if decision not in ("ok", "error"):
        return JSONResponse({"error": "invalid decision"}, status_code=400)
    conn = get_db()
    conn.execute(
        "UPDATE results SET manual_decision=? WHERE id=?", (decision, result_id)
    )
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ─── Routes: Debug OCR ───────────────────────────────────────────────────────

@app.post("/debug/ocr")
async def debug_ocr(zip_file: UploadFile = File(...)):
    zip_bytes = await zip_file.read()
    contents = process_zip(zip_bytes)
    langs = sorted(contents.images.keys())
    if not langs:
        return JSONResponse({"error": "no images found"})

    results = []
    for lang in langs[:2]:
        image_bytes = contents.images[lang]
        image_name = contents.image_names[lang]

        ocr_result = await asyncio.get_event_loop().run_in_executor(None, run_ocr, image_bytes)
        ocr_text = ocr_result.text

        sections_data = []
        ref_info = {}
        if lang in contents.texts:
            fname, file_bytes = contents.texts[lang]
            sections = await asyncio.get_event_loop().run_in_executor(
                None, extract_sections, file_bytes, fname
            )
            selection = await asyncio.get_event_loop().run_in_executor(
                None, select_best, sections, ocr_text, lang, None, None
            )
            for s in sections:
                sections_data.append({
                    "number": s.number,
                    "name": s.name,
                    "content_preview": s.content_text[:80],
                    "norm_strict": normalize_strict(s.content_text),
                })
            if selection.best:
                ref_info = {
                    "matched_section": selection.best.section.name,
                    "status": selection.status,
                    "reason": selection.reason,
                    "strict_equal": selection.best.strict_equal,
                    "ocr_norm": normalize_strict(ocr_text),
                    "ref_norm": normalize_strict(selection.best.section.content_text),
                }

        results.append({
            "lang": lang,
            "image_name": image_name,
            "ocr_text_raw": ocr_text,
            "ocr_text_display": clean_for_display(ocr_text),
            "ocr_confidence": ocr_result.confidence,
            "ocr_engine": ocr_result.engine,
            "sections": sections_data,
            "match": ref_info,
        })

    return JSONResponse(results)


# ─── Routes: Download error images ZIP ───────────────────────────────────────

@app.get("/download/{session_id}")
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
