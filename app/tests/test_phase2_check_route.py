import asyncio
import os

os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app import main


class _Req:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def test_phase2_check_passes_target_bboxes_to_session_start(monkeypatch, tmp_path):
    db_path = tmp_path / "phase2_check.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    main.init_db()

    conn = main.get_db()
    conn.execute(
        "INSERT INTO phase2_uploads (upload_id, created_at, zip_bytes, section_number, section_name) VALUES (?,?,?,?,?)",
        ("u1", 9999999999.0, b"zip", 7, "PIC"),
    )
    conn.commit()
    conn.close()

    import app.pipeline.phase2_routes as phase2_routes
    monkeypatch.setattr(phase2_routes, "_resolve_target_bboxes", lambda _name: {"700": [1, 2, 30, 40]})

    captured = {}

    def _fake_start(zip_bytes, section_number, section_name, engines, target_bboxes=None):
        captured["zip_bytes"] = zip_bytes
        captured["section_number"] = section_number
        captured["section_name"] = section_name
        captured["engines"] = engines
        captured["target_bboxes"] = target_bboxes
        return "session-1"

    monkeypatch.setattr(main, "_start_session_from_zip", _fake_start)

    response = asyncio.run(main.phase2_check("u1", _Req({"template_name": "archive.zip"})))

    assert response.status_code == 200
    assert response.body == b'{"session_id":"session-1","engines":["google","azure","ocrspace"]}'
    assert captured["section_number"] == 7
    assert captured["section_name"] == "PIC"
    assert captured["target_bboxes"] == {"700": [1, 2, 30, 40]}
