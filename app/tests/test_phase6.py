import io
import re
import sys
import types
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.pipeline import history_routes, persistence, run_routes
from app.pipeline.models import ZoneDef


class DummyTemplate:
    def __init__(self, zones):
        self.zones = zones
        self.source_size = [10, 10]
        self.expected_texts = {}


def _png_bytes():
    img = Image.new("RGB", (10, 10), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _run_client():
    app = FastAPI()
    app.include_router(run_routes.run_router)
    return TestClient(app)


def _history_client():
    app = FastAPI()
    app.include_router(history_routes.history_router)
    return TestClient(app)


def _install_fake_firestore(monkeypatch):
    firestore_mod = types.ModuleType("google.cloud.firestore")

    def transactional(fn):
        return fn

    firestore_mod.transactional = transactional
    firestore_mod.SERVER_TIMESTAMP = object()

    google_mod = types.ModuleType("google")
    cloud_mod = types.ModuleType("google.cloud")
    cloud_mod.firestore = firestore_mod
    google_mod.cloud = cloud_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud_mod)
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", firestore_mod)
    return firestore_mod


def test_run_persisted_flags_success(monkeypatch):
    zone = ZoneDef(name="z1", type="logo", bbox=[0, 0, 5, 5], engines=[])
    monkeypatch.setattr(run_routes.template_store, "get_template", lambda _: DummyTemplate([zone]))
    monkeypatch.setattr(run_routes, "build_validation_result", lambda **_: ({"ok": True}, None))
    monkeypatch.setattr(run_routes, "resolve_consensus", lambda **_: {"zone_status": "OK", "reason": None})
    monkeypatch.setattr(run_routes, "FIRESTORE_AVAILABLE", True)
    monkeypatch.setattr(
        run_routes,
        "persist_run",
        lambda **_: {"persisted": True, "persistence_error": False, "persistence_error_type": None},
    )

    resp = _run_client().post(
        "/api/templates/t1/run",
        files={"image": ("img.png", _png_bytes(), "image/png")},
    )
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["persisted"] is True
    assert payload["persistence_error"] is False
    assert payload["persistence_error_type"] is None


def test_run_persistence_failure_returns_200(monkeypatch):
    zone = ZoneDef(name="z1", type="logo", bbox=[0, 0, 5, 5], engines=[])
    monkeypatch.setattr(run_routes.template_store, "get_template", lambda _: DummyTemplate([zone]))
    monkeypatch.setattr(run_routes, "build_validation_result", lambda **_: ({"ok": True}, None))
    monkeypatch.setattr(run_routes, "resolve_consensus", lambda **_: {"zone_status": "OK", "reason": None})
    monkeypatch.setattr(run_routes, "FIRESTORE_AVAILABLE", True)
    monkeypatch.setattr(
        run_routes,
        "persist_run",
        lambda **_: {
            "persisted": False,
            "persistence_error": True,
            "persistence_error_type": "firestore_write_failed",
        },
    )

    resp = _run_client().post(
        "/api/templates/t1/run",
        files={"image": ("img.png", _png_bytes(), "image/png")},
    )
    payload = resp.json()

    assert resp.status_code == 200
    assert payload["persisted"] is False
    assert payload["persistence_error"] is True
    assert payload["persistence_error_type"] == "firestore_write_failed"
    assert "zones" in payload


def test_persist_run_batch_commit_once_and_payload_preserved(monkeypatch):
    _install_fake_firestore(monkeypatch)
    monkeypatch.setattr(persistence, "FIRESTORE_AVAILABLE", True)

    calls = {"commit": 0, "sets": []}

    class Batch:
        def set(self, ref, data):
            calls["sets"].append((ref, data))

        def commit(self):
            calls["commit"] += 1

    class DB:
        def batch(self):
            return Batch()

        def collection(self, name):
            class C:
                def document(self, doc_id):
                    return (name, doc_id)

            return C()

    monkeypatch.setattr(persistence, "get_db", lambda: DB())
    monkeypatch.setattr(persistence, "log_event", lambda *_, **__: None)

    zone = {
        "zone_name": "z1",
        "consensus": {"zone_status": "OK"},
        "extra_unknown": {"nested": [1, 2, 3]},
    }
    out = persistence.persist_run("r1", "tpl", "en", [zone])

    assert out == {"persisted": True, "persistence_error": False, "persistence_error_type": None}
    assert calls["commit"] == 1
    zone_write = calls["sets"][1][1]
    assert zone_write["run_zone_payload"]["extra_unknown"] == {"nested": [1, 2, 3]}


def test_history_unknown_template_empty_runs(monkeypatch):
    class Query:
        def where(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def stream(self):
            return []

    class DB:
        def collection(self, *_):
            return Query()

    monkeypatch.setattr(history_routes, "get_db", lambda: DB())
    resp = _history_client().get("/api/templates/unknown/history")
    assert resp.status_code == 200
    assert resp.json() == {"template_name": "unknown", "runs": []}


def test_history_timestamps_iso_z_no_microseconds(monkeypatch):
    ts = datetime(2024, 1, 2, 3, 4, 5, 987654, tzinfo=timezone.utc)

    class Doc:
        def to_dict(self):
            return {
                "run_id": "r1",
                "created_at": ts,
                "lang": "en",
                "zones_count": 1,
                "ocr_overall_status": "OK",
                "review_overall_status": "PENDING",
            }

    class Query:
        def where(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def stream(self):
            return [Doc()]

    class DB:
        def collection(self, *_):
            return Query()

    monkeypatch.setattr(history_routes, "get_db", lambda: DB())
    resp = _history_client().get("/api/templates/tpl/history")
    created = resp.json()["runs"][0]["created_at_utc"]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", created)


class _Snap:
    def __init__(self, exists, data):
        self.exists = exists
        self._data = data

    def to_dict(self):
        return self._data


class _Ref:
    def __init__(self, snap):
        self.snap = snap


class _Txn:
    def __init__(self):
        self.updates = []

    def get(self, ref):
        return ref.snap

    def update(self, ref, data):
        self.updates.append((ref, data))


class _DocCollection:
    def __init__(self, docs):
        self.docs = docs

    def document(self, doc_id):
        return _Ref(self.docs.get(doc_id, _Snap(False, {})))


class _ReviewDB:
    def __init__(self, header_snap, zone_snap, txn):
        self.header_snap = header_snap
        self.zone_snap = zone_snap
        self.txn = txn

    def collection(self, name):
        if name == persistence.COLLECTION_RUNS:
            return _DocCollection({"r1": self.header_snap})
        if name == persistence.COLLECTION_ZONES:
            return _DocCollection({"r1__0": self.zone_snap})
        raise AssertionError("unexpected collection")

    def transaction(self):
        return self.txn


def test_review_invalid_status_400():
    resp = _history_client().post("/api/runs/r1/zones/0/review", json={"review_status": "PENDING"})
    assert resp.status_code == 400


def test_review_comment_too_long_400():
    body = {"review_status": "APPROVED", "review_comment": "x" * 1001}
    resp = _history_client().post("/api/runs/r1/zones/0/review", json=body)
    assert resp.status_code == 400


def test_review_zone_not_found_404(monkeypatch):
    _install_fake_firestore(monkeypatch)
    txn = _Txn()
    db = _ReviewDB(
        _Snap(True, {"review_counts": {"pending": 1, "approved": 0, "rejected": 0}}),
        _Snap(False, {}),
        txn,
    )
    monkeypatch.setattr(history_routes, "get_db", lambda: db)

    resp = _history_client().post("/api/runs/r1/zones/0/review", json={"review_status": "APPROVED"})
    assert resp.status_code == 404
    assert resp.json() == {"error": "zone_not_found"}


def test_review_idempotent_approved_to_approved(monkeypatch):
    fake_fs = _install_fake_firestore(monkeypatch)
    txn = _Txn()
    db = _ReviewDB(
        _Snap(True, {"review_counts": {"pending": 0, "approved": 1, "rejected": 0}}),
        _Snap(True, {"review": {"review_status": "APPROVED"}}),
        txn,
    )
    monkeypatch.setattr(history_routes, "get_db", lambda: db)
    monkeypatch.setattr(history_routes, "log_event", lambda *_, **__: None)

    resp = _history_client().post(
        "/api/runs/r1/zones/0/review",
        json={"review_status": "APPROVED", "review_comment": "same"},
    )
    assert resp.status_code == 200

    header_update = txn.updates[1][1]
    zone_update = txn.updates[0][1]
    assert header_update["review_counts"] == {"pending": 0, "approved": 1, "rejected": 0}
    assert header_update["review_overall_status"] == "APPROVED"
    assert zone_update["review.reviewed_at"] is fake_fs.SERVER_TIMESTAMP


def test_review_counters_and_overall_derived(monkeypatch):
    _install_fake_firestore(monkeypatch)
    txn = _Txn()
    db = _ReviewDB(
        _Snap(True, {"review_counts": {"pending": 2, "approved": 0, "rejected": 0}}),
        _Snap(True, {"review": {"review_status": "PENDING"}}),
        txn,
    )
    monkeypatch.setattr(history_routes, "get_db", lambda: db)
    monkeypatch.setattr(history_routes, "log_event", lambda *_, **__: None)

    resp = _history_client().post("/api/runs/r1/zones/0/review", json={"review_status": "REJECTED"})
    assert resp.status_code == 200

    header_update = txn.updates[1][1]
    assert header_update["review_counts"] == {"pending": 1, "approved": 0, "rejected": 1}
    assert header_update["review_overall_status"] == "REJECTED"
