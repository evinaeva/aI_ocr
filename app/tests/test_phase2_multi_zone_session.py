import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app import main


def _seed_session(tmp_path, monkeypatch, session_id: str = "s-multi") -> str:
    db_path = tmp_path / "session_multi.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    main.init_db()
    conn = main.get_db()
    conn.execute(
        "INSERT INTO sessions (session_id, created_at, status, total, pass_count, fail_count, manual_count, engines) VALUES (?,?,?,?,?,?,?,?)",
        (session_id, 0.0, "pending", 0, 0, 0, 0, "google"),
    )
    conn.commit()
    conn.close()
    return session_id


def test_process_session_processes_all_zones_and_preserves_zone_reference(monkeypatch, tmp_path):
    session_id = _seed_session(tmp_path, monkeypatch, "s-multi-1")

    manifest = [
        SimpleNamespace(
            items=[
                SimpleNamespace(
                    archive_path="700/en.png",
                    lang="en",
                    target_id="700",
                    zone_name="headline",
                    bbox=[0, 0, 10, 10],
                    expected_by_lang={"en": "Header text"},
                ),
                SimpleNamespace(
                    archive_path="700/en.png",
                    lang="en",
                    target_id="700",
                    zone_name="cta",
                    bbox=[10, 10, 20, 20],
                    expected_by_lang={"en": "CTA text"},
                ),
            ]
        )
    ]

    monkeypatch.setattr(main, "process_zip", lambda _zip: SimpleNamespace(texts={}, images={}, image_names={}))
    monkeypatch.setattr(main, "build_zip_manifest", lambda _zip, **_kwargs: manifest)
    monkeypatch.setattr(main, "_collect_zip_debug_counters", lambda _zip: {"images_skipped_by_reason": {}})
    monkeypatch.setattr(main, "_read_archive_image", lambda *_args, **_kwargs: b"img")
    monkeypatch.setattr(main, "_push_event", lambda *_args, **_kwargs: None)

    crop1 = SimpleNamespace(bytes=b"c1", bbox=[0, 0, 10, 10])
    crop2 = SimpleNamespace(bytes=b"c2", bbox=[10, 10, 20, 20])
    monkeypatch.setattr(
        main,
        "_prefetch_google_for_zip_items",
        lambda *_args, **_kwargs: ({0: b"img", 1: b"img"}, {0: crop1, 1: crop2}, set(), set(), set(), []),
    )

    calls = {"count": 0}

    def _fake_ocr(crop, _engines):
        calls["count"] += 1
        return {"google": {"text": f"text-{calls['count']}", "confidence": 0.9}}

    monkeypatch.setattr(main, "_run_zone_ocr_for_engines", _fake_ocr)

    asyncio.run(main._process_session(session_id, b"zip", None, None, ["google"]))

    conn = main.get_db()
    rows = conn.execute(
        "SELECT zone_name, ref_text, reason, target_id FROM results WHERE session_id=? ORDER BY id",
        (session_id,),
    ).fetchall()
    conn.close()

    assert calls["count"] == 2
    assert [r["zone_name"] for r in rows] == ["headline", "cta"]
    assert [r["ref_text"] for r in rows] == ["Header text", "CTA text"]
    assert all(r["target_id"] == "700" for r in rows)


def test_missing_zone_bbox_only_skips_that_zone(monkeypatch, tmp_path):
    session_id = _seed_session(tmp_path, monkeypatch, "s-multi-2")

    manifest = [
        SimpleNamespace(
            items=[
                SimpleNamespace(
                    archive_path="700/en.png",
                    lang="en",
                    target_id="700",
                    zone_name="headline",
                    bbox=[0, 0, 10, 10],
                    expected_by_lang={"en": "Header text"},
                ),
                SimpleNamespace(
                    archive_path="700/en.png",
                    lang="en",
                    target_id="700",
                    zone_name="cta",
                    bbox=None,
                    expected_by_lang={"en": "CTA text"},
                ),
            ]
        )
    ]

    monkeypatch.setattr(main, "process_zip", lambda _zip: SimpleNamespace(texts={}, images={}, image_names={}))
    monkeypatch.setattr(main, "build_zip_manifest", lambda _zip, **_kwargs: manifest)
    monkeypatch.setattr(main, "_collect_zip_debug_counters", lambda _zip: {"images_skipped_by_reason": {}})
    monkeypatch.setattr(main, "_read_archive_image", lambda *_args, **_kwargs: b"img")
    monkeypatch.setattr(main, "_push_event", lambda *_args, **_kwargs: None)
    crop1 = SimpleNamespace(bytes=b"c1", bbox=[0, 0, 10, 10])
    monkeypatch.setattr(
        main,
        "_prefetch_google_for_zip_items",
        lambda *_args, **_kwargs: ({0: b"img", 1: b"img"}, {0: crop1}, set(), {1}, set(), []),
    )
    monkeypatch.setattr(main, "_run_zone_ocr_for_engines", lambda *_args, **_kwargs: {"google": {"text": "ok", "confidence": 0.9}})

    asyncio.run(main._process_session(session_id, b"zip", None, None, ["google"]))

    conn = main.get_db()
    rows = conn.execute(
        "SELECT zone_name, reason, status FROM results WHERE session_id=? ORDER BY id",
        (session_id,),
    ).fetchall()
    conn.close()

    assert len(rows) == 2
    assert rows[0]["zone_name"] == "headline"
    assert rows[0]["reason"] != "missing_bbox"
    assert rows[1]["zone_name"] == "cta"
    assert rows[1]["reason"] == "missing_bbox"
    assert rows[1]["status"] == "MANUAL"
