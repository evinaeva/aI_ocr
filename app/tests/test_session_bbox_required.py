import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app import main


def test_resolve_real_bbox_accepts_tuple():
    item = SimpleNamespace(bbox=(1, 2, 30, 40))
    assert main._resolve_real_bbox(item) == [1, 2, 30, 40]


def test_resolve_real_bbox_accepts_list():
    item = SimpleNamespace(bbox=[1, 2, 30, 40])
    assert main._resolve_real_bbox(item) == [1, 2, 30, 40]


def test_resolve_real_bbox_rejects_invalid_types():
    assert main._resolve_real_bbox(SimpleNamespace(bbox="1,2,3,4")) is None
    assert main._resolve_real_bbox(SimpleNamespace(bbox=b"1234")) is None
    assert main._resolve_real_bbox(SimpleNamespace(bbox=[1, 2, "x", 4])) is None
    assert main._resolve_real_bbox(SimpleNamespace(bbox=[1, 2, 3])) is None
    assert main._resolve_real_bbox(SimpleNamespace(bbox=[1, 2, 3, 4.1])) is None


def test_process_session_missing_bbox_marks_manual_and_skips_ocr(monkeypatch, tmp_path):
    db_path = tmp_path / "session.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    main.init_db()

    session_id = "s1"
    conn = main.get_db()
    conn.execute(
        "INSERT INTO sessions (session_id, created_at, status, total, pass_count, fail_count, manual_count, engines) VALUES (?,?,?,?,?,?,?,?)",
        (session_id, 0.0, "pending", 0, 0, 0, 0, "google"),
    )
    conn.commit()
    conn.close()

    item = SimpleNamespace(archive_path="images/a.png", lang="en")
    manifest = [SimpleNamespace(items=[item])]

    monkeypatch.setattr(main, "process_zip", lambda _zip: SimpleNamespace(texts={}, images={}, image_names={}))
    monkeypatch.setattr(main, "build_zip_manifest", lambda _zip, **_kwargs: manifest)
    monkeypatch.setattr(
        main,
        "_collect_zip_debug_counters",
        lambda _zip: {
            "zip_entries_total": 1,
            "images_detected_total": 1,
            "images_queued_total": 1,
            "images_processed_total": 0,
            "images_skipped_total": 0,
            "images_skipped_by_reason": {},
        },
    )
    monkeypatch.setattr(
        main,
        "_prefetch_google_for_zip_items",
        lambda *_args, **_kwargs: ({0: b"img"}, {}, set(), {0}, set(), []),
    )
    monkeypatch.setattr(
        main,
        "_run_zone_ocr_for_engines",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("OCR must not run without bbox")),
    )

    pushed_events = []
    monkeypatch.setattr(main, "_push_event", lambda _sid, event: pushed_events.append(event))

    asyncio.run(main._process_session(session_id, b"zip", None, None, ["google"]))

    conn = main.get_db()
    row = conn.execute(
        "SELECT status, reason FROM results WHERE session_id=? ORDER BY id LIMIT 1",
        (session_id,),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["status"] == "MANUAL"
    assert row["reason"] == "missing_bbox"
    assert any(evt.get("event") == "item" and evt.get("reason") == "missing_bbox" for evt in pushed_events)
    payload = asyncio.run(main.get_results(session_id))
    body = payload.body.decode("utf-8")
    assert '"results"' in body
    assert '"ocr_results":{}' in body


def test_process_session_tuple_bbox_not_marked_missing_bbox(monkeypatch, tmp_path):
    db_path = tmp_path / "session_tuple.db"
    monkeypatch.setattr(main, "DB_PATH", str(db_path))
    main.init_db()

    session_id = "s2"
    conn = main.get_db()
    conn.execute(
        "INSERT INTO sessions (session_id, created_at, status, total, pass_count, fail_count, manual_count, engines) VALUES (?,?,?,?,?,?,?,?)",
        (session_id, 0.0, "pending", 0, 0, 0, 0, "google"),
    )
    conn.commit()
    conn.close()

    item = SimpleNamespace(archive_path="images/a.png", lang="en", bbox=(10, 10, 60, 60))
    manifest = [SimpleNamespace(items=[item])]

    monkeypatch.setattr(main, "process_zip", lambda _zip: SimpleNamespace(texts={}, images={}, image_names={}))
    monkeypatch.setattr(main, "build_zip_manifest", lambda _zip, **_kwargs: manifest)
    monkeypatch.setattr(
        main,
        "_collect_zip_debug_counters",
        lambda _zip: {
            "zip_entries_total": 1,
            "images_detected_total": 1,
            "images_queued_total": 1,
            "images_processed_total": 0,
            "images_skipped_total": 0,
            "images_skipped_by_reason": {},
        },
    )
    monkeypatch.setattr(main, "_read_archive_image", lambda *_args, **_kwargs: b"img")
    monkeypatch.setattr(main, "_crop_zip_zone", lambda *_args, **_kwargs: "crop")
    ocr_calls = {"count": 0}
    monkeypatch.setattr(main, "_push_event", lambda *_args, **_kwargs: None)

    def _fake_ocr(*_args, **_kwargs):
        ocr_calls["count"] += 1
        return {}

    monkeypatch.setattr(main, "_run_zone_ocr_for_engines", _fake_ocr)

    asyncio.run(main._process_session(session_id, b"zip", None, None, ["google"]))

    conn = main.get_db()
    row = conn.execute(
        "SELECT reason FROM results WHERE session_id=? ORDER BY id LIMIT 1",
        (session_id,),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["reason"] != "missing_bbox"
    assert ocr_calls["count"] >= 1
