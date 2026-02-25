"""
Phase 6 tests — mocks only, no real Firestore.

Covers (per spec §11):
- /run returns persisted flags on success and failure
- persistence failure returns 200 + persisted=False + error_type
- batched write called once (no partial multi-commit)
- run_zone_payload preserves unknown extra fields
- review update idempotency (APPROVED->APPROVED allowed)
- invalid status -> 400
- comment length limit -> 400
- zone not found -> 404
- counters update correctly and review_overall_status derived correctly
- history returns empty list for unknown template_name (200)
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_zone(name="headline", status="OK"):
    return {
        "zone_name": name,
        "engines_used": ["google"],
        "engine_results": [],
        "consensus": {"zone_status": status, "zone_name": name},
        "validation": {},
        "extra_unknown_field": "preserved",
    }


def _make_run_id():
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# persistence.py unit tests
# ---------------------------------------------------------------------------

class TestPersistRun:
    """Tests for app.pipeline.persistence.persist_run()"""

    def _mock_db(self):
        db = MagicMock()
        batch = MagicMock()
        db.batch.return_value = batch
        db.collection.return_value.document.return_value = MagicMock()
        return db, batch

    def test_success_returns_persisted_true(self):
        from app.pipeline import persistence

        db, batch = self._mock_db()
        run_id = _make_run_id()
        zones = [_make_zone("headline", "OK"), _make_zone("banner", "MANUAL")]

        with patch.object(persistence, "FIRESTORE_AVAILABLE", True), \
             patch.object(persistence, "get_db", return_value=db), \
             patch("app.pipeline.persistence.get_db", return_value=db):
            # patch google.cloud.firestore inside the function
            import sys
            mock_fs = MagicMock()
            mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
            with patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                             "google.cloud.firestore": mock_fs}):
                result = persistence.persist_run(run_id, "test_tmpl", "ru", zones)

        assert result["persisted"] is True
        assert result["persistence_error"] is False
        assert result["persistence_error_type"] is None

    def test_batch_commit_called_exactly_once(self):
        from app.pipeline import persistence

        db, batch = self._mock_db()
        run_id = _make_run_id()
        zones = [_make_zone(), _make_zone("footer")]

        with patch.object(persistence, "FIRESTORE_AVAILABLE", True), \
             patch.object(persistence, "get_db", return_value=db):
            mock_fs = MagicMock()
            mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
            with patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                             "google.cloud.firestore": mock_fs}):
                persistence.persist_run(run_id, "tmpl", "en", zones)

        # Exactly one commit (§1.2 / §3.1)
        batch.commit.assert_called_once()

    def test_commit_failure_returns_persisted_false(self):
        from app.pipeline import persistence

        db, batch = self._mock_db()
        batch.commit.side_effect = Exception("network error")
        run_id = _make_run_id()

        with patch.object(persistence, "FIRESTORE_AVAILABLE", True), \
             patch.object(persistence, "get_db", return_value=db):
            mock_fs = MagicMock()
            mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
            with patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                             "google.cloud.firestore": mock_fs}):
                result = persistence.persist_run(run_id, "tmpl", None, [_make_zone()])

        assert result["persisted"] is False
        assert result["persistence_error"] is True
        assert result["persistence_error_type"] == "firestore_write_failed"

    def test_firestore_unavailable_returns_persisted_false(self):
        from app.pipeline import persistence

        with patch.object(persistence, "FIRESTORE_AVAILABLE", False):
            result = persistence.persist_run(_make_run_id(), "tmpl", None, [])

        assert result["persisted"] is False
        assert result["persistence_error"] is True
        assert result["persistence_error_type"] == "firestore_write_failed"

    def test_ocr_overall_status_manual_if_any_manual(self):
        from app.pipeline import persistence

        db, batch = self._mock_db()
        zones = [_make_zone("z1", "OK"), _make_zone("z2", "MANUAL")]

        with patch.object(persistence, "FIRESTORE_AVAILABLE", True), \
             patch.object(persistence, "get_db", return_value=db):
            mock_fs = MagicMock()
            mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
            with patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                             "google.cloud.firestore": mock_fs}):
                persistence.persist_run(_make_run_id(), "t", "en", zones)

        # Find the header set call — first batch.set call
        header_data = batch.set.call_args_list[0][0][1]
        assert header_data["ocr_overall_status"] == "MANUAL"

    def test_ocr_overall_status_ok_if_all_ok(self):
        from app.pipeline import persistence

        db, batch = self._mock_db()
        zones = [_make_zone("z1", "OK"), _make_zone("z2", "OK")]

        with patch.object(persistence, "FIRESTORE_AVAILABLE", True), \
             patch.object(persistence, "get_db", return_value=db):
            mock_fs = MagicMock()
            mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
            with patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                             "google.cloud.firestore": mock_fs}):
                persistence.persist_run(_make_run_id(), "t", "en", zones)

        header_data = batch.set.call_args_list[0][0][1]
        assert header_data["ocr_overall_status"] == "OK"

    def test_run_zone_payload_preserves_unknown_fields(self):
        """run_zone_payload must preserve unknown additive fields (§2.1)."""
        from app.pipeline import persistence

        db, batch = self._mock_db()
        zone = _make_zone()
        zone["extra_unknown_field"] = "i_must_survive"

        with patch.object(persistence, "FIRESTORE_AVAILABLE", True), \
             patch.object(persistence, "get_db", return_value=db):
            mock_fs = MagicMock()
            mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
            with patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                             "google.cloud.firestore": mock_fs}):
                persistence.persist_run(_make_run_id(), "t", None, [zone])

        # Zone doc is the second batch.set call (index 1)
        zone_data = batch.set.call_args_list[1][0][1]
        assert zone_data["run_zone_payload"]["extra_unknown_field"] == "i_must_survive"

    def test_initial_review_counts_all_pending(self):
        from app.pipeline import persistence

        db, batch = self._mock_db()
        zones = [_make_zone("z1"), _make_zone("z2"), _make_zone("z3")]

        with patch.object(persistence, "FIRESTORE_AVAILABLE", True), \
             patch.object(persistence, "get_db", return_value=db):
            mock_fs = MagicMock()
            mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"
            with patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                             "google.cloud.firestore": mock_fs}):
                persistence.persist_run(_make_run_id(), "t", "en", zones)

        header_data = batch.set.call_args_list[0][0][1]
        assert header_data["review_counts"] == {"pending": 3, "approved": 0, "rejected": 0}
        assert header_data["review_overall_status"] == "PENDING"


# ---------------------------------------------------------------------------
# history_routes.py unit tests (via FastAPI TestClient)
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Create TestClient with Firestore mocked at module level."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.pipeline.history_routes import history_router

    test_app = FastAPI()
    test_app.include_router(history_router)
    return TestClient(test_app)


class TestHistoryEndpoint:

    def test_empty_history_returns_200_not_404(self, client):
        """§6.1: unknown template_name returns 200 with empty runs list."""
        mock_db = MagicMock()
        mock_query = MagicMock()
        mock_db.collection.return_value.where.return_value.order_by.return_value.limit.return_value = mock_query
        mock_query.stream.return_value = []  # no docs

        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True), \
             patch.object(history_routes, "get_db", return_value=mock_db):
            resp = client.get("/api/templates/nonexistent_template/history")

        assert resp.status_code == 200
        body = resp.json()
        assert body["template_name"] == "nonexistent_template"
        assert body["runs"] == []

    def test_history_returns_run_summaries(self, client):
        mock_db = MagicMock()
        run_id = _make_run_id()

        mock_doc = MagicMock()
        mock_doc.to_dict.return_value = {
            "run_id": run_id,
            "created_at": None,
            "lang": "ru",
            "zones_count": 3,
            "ocr_overall_status": "OK",
            "review_overall_status": "PENDING",
        }
        mock_query = MagicMock()
        mock_db.collection.return_value.where.return_value.order_by.return_value.limit.return_value = mock_query
        mock_query.stream.return_value = [mock_doc]

        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True), \
             patch.object(history_routes, "get_db", return_value=mock_db):
            resp = client.get("/api/templates/my_tmpl/history")

        assert resp.status_code == 200
        runs = resp.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id
        assert runs[0]["lang"] == "ru"
        assert runs[0]["zones_count"] == 3


class TestGetRunEndpoint:

    def test_404_for_missing_run(self, client):
        mock_db = MagicMock()
        snap = MagicMock()
        snap.exists = False
        mock_db.collection.return_value.document.return_value.get.return_value = snap

        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True), \
             patch.object(history_routes, "get_db", return_value=mock_db):
            resp = client.get(f"/api/runs/{_make_run_id()}")

        assert resp.status_code == 404

    def test_full_run_zones_ordered_by_index(self, client):
        mock_db = MagicMock()
        run_id = _make_run_id()

        header_snap = MagicMock()
        header_snap.exists = True
        header_snap.to_dict.return_value = {
            "run_id": run_id,
            "template_name": "tmpl",
            "created_at": None,
            "lang": "en",
            "ocr_overall_status": "OK",
            "review_overall_status": "PENDING",
        }

        zone_docs = []
        for i in range(3):
            zdoc = MagicMock()
            zdoc.to_dict.return_value = {
                "run_id": run_id,
                "zone_index": i,
                "zone_name": f"zone_{i}",
                "run_zone_payload": {"zone_name": f"zone_{i}"},
                "review": {"review_status": "PENDING", "review_comment": None, "reviewed_at": None},
            }
            zone_docs.append(zdoc)

        mock_db.collection.return_value.document.return_value.get.return_value = header_snap
        mock_db.collection.return_value.where.return_value.order_by.return_value.stream.return_value = zone_docs

        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True), \
             patch.object(history_routes, "get_db", return_value=mock_db):
            resp = client.get(f"/api/runs/{run_id}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == run_id
        assert len(body["zones"]) == 3
        assert body["zones"][0]["zone_index"] == 0
        assert body["zones"][2]["zone_index"] == 2


class TestReviewEndpoint:

    def _setup_review_mock(self, prev_status="PENDING", counts=None):
        if counts is None:
            counts = {"pending": 1, "approved": 0, "rejected": 0}

        mock_db = MagicMock()
        run_id = _make_run_id()

        header_snap = MagicMock()
        header_snap.exists = True
        header_snap.to_dict.return_value = {
            "review_counts": counts,
            "review_overall_status": "PENDING",
        }

        zone_snap = MagicMock()
        zone_snap.exists = True
        zone_snap.to_dict.return_value = {
            "review": {"review_status": prev_status, "review_comment": None, "reviewed_at": None}
        }

        # transaction mock
        txn = MagicMock()

        def mock_transactional(fn):
            def wrapper(transaction):
                return fn(transaction)
            return wrapper

        mock_fs = MagicMock()
        mock_fs.transactional = mock_transactional
        mock_fs.SERVER_TIMESTAMP = "SERVER_TIMESTAMP"

        # Simulate get() inside transaction returning the snaps
        header_ref = MagicMock()
        header_ref.get.return_value = header_snap
        zone_ref = MagicMock()
        zone_ref.get.return_value = zone_snap

        def collection_side(name):
            col = MagicMock()
            if name == "template_runs":
                col.document.return_value = header_ref
            else:
                col.document.return_value = zone_ref
            return col

        mock_db.collection.side_effect = collection_side
        mock_db.transaction.return_value = txn

        return mock_db, mock_fs, run_id

    def test_invalid_status_returns_400(self, client):
        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True):
            resp = client.post(
                f"/api/runs/{_make_run_id()}/zones/0/review",
                json={"review_status": "INVALID"}
            )
        assert resp.status_code == 400

    def test_comment_too_long_returns_400(self, client):
        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True):
            resp = client.post(
                f"/api/runs/{_make_run_id()}/zones/0/review",
                json={"review_status": "APPROVED", "review_comment": "x" * 1001}
            )
        assert resp.status_code == 400

    def test_zone_not_found_returns_404(self, client):
        mock_db = MagicMock()

        header_snap = MagicMock()
        header_snap.exists = True
        header_snap.to_dict.return_value = {"review_counts": {"pending": 1, "approved": 0, "rejected": 0}}

        zone_snap = MagicMock()
        zone_snap.exists = False

        header_ref = MagicMock()
        header_ref.get.return_value = header_snap
        zone_ref = MagicMock()
        zone_ref.get.return_value = zone_snap

        def collection_side(name):
            col = MagicMock()
            if name == "template_runs":
                col.document.return_value = header_ref
            else:
                col.document.return_value = zone_ref
            return col

        mock_db.collection.side_effect = collection_side
        mock_db.transaction.return_value = MagicMock()

        mock_fs = MagicMock()
        mock_fs.transactional = lambda fn: (lambda txn: fn(txn))
        mock_fs.SERVER_TIMESTAMP = "ts"

        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True), \
             patch.object(history_routes, "get_db", return_value=mock_db), \
             patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                        "google.cloud.firestore": mock_fs}):
            resp = client.post(
                f"/api/runs/{_make_run_id()}/zones/0/review",
                json={"review_status": "APPROVED"}
            )
        assert resp.status_code == 404
        assert resp.json()["error"] == "zone_not_found"

    def test_counters_update_pending_to_approved(self, client):
        """PENDING -> APPROVED: pending-1, approved+1, overall=APPROVED."""
        mock_db = MagicMock()

        counts_written = {}

        header_snap = MagicMock()
        header_snap.exists = True
        header_snap.to_dict.return_value = {
            "review_counts": {"pending": 1, "approved": 0, "rejected": 0}
        }

        zone_snap = MagicMock()
        zone_snap.exists = True
        zone_snap.to_dict.return_value = {
            "review": {"review_status": "PENDING"}
        }

        header_ref = MagicMock()
        header_ref.get.return_value = header_snap
        zone_ref = MagicMock()
        zone_ref.get.return_value = zone_snap

        txn = MagicMock()

        def capture_update(ref, data):
            if "review_counts" in data:
                counts_written.update(data)

        txn.update.side_effect = lambda ref, data: counts_written.update(data) if "review_counts" in data else None

        def collection_side(name):
            col = MagicMock()
            if name == "template_runs":
                col.document.return_value = header_ref
            else:
                col.document.return_value = zone_ref
            return col

        mock_db.collection.side_effect = collection_side
        mock_db.transaction.return_value = txn

        mock_fs = MagicMock()
        mock_fs.transactional = lambda fn: (lambda t: fn(t))
        mock_fs.SERVER_TIMESTAMP = "ts"

        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True), \
             patch.object(history_routes, "get_db", return_value=mock_db), \
             patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                        "google.cloud.firestore": mock_fs}):
            resp = client.post(
                f"/api/runs/{_make_run_id()}/zones/0/review",
                json={"review_status": "APPROVED"}
            )

        assert resp.status_code == 200
        assert counts_written.get("review_counts") == {"pending": 0, "approved": 1, "rejected": 0}
        assert counts_written.get("review_overall_status") == "APPROVED"

    def test_idempotency_approved_to_approved(self, client):
        """APPROVED -> APPROVED: counts should stay consistent, no error (§idempotency)."""
        mock_db = MagicMock()
        counts_written = {}

        header_snap = MagicMock()
        header_snap.exists = True
        header_snap.to_dict.return_value = {
            "review_counts": {"pending": 0, "approved": 1, "rejected": 0}
        }

        zone_snap = MagicMock()
        zone_snap.exists = True
        zone_snap.to_dict.return_value = {
            "review": {"review_status": "APPROVED"}
        }

        header_ref = MagicMock()
        header_ref.get.return_value = header_snap
        zone_ref = MagicMock()
        zone_ref.get.return_value = zone_snap

        txn = MagicMock()
        txn.update.side_effect = lambda ref, data: counts_written.update(data) if "review_counts" in data else None

        def collection_side(name):
            col = MagicMock()
            if name == "template_runs":
                col.document.return_value = header_ref
            else:
                col.document.return_value = zone_ref
            return col

        mock_db.collection.side_effect = collection_side
        mock_db.transaction.return_value = txn

        mock_fs = MagicMock()
        mock_fs.transactional = lambda fn: (lambda t: fn(t))
        mock_fs.SERVER_TIMESTAMP = "ts"

        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True), \
             patch.object(history_routes, "get_db", return_value=mock_db), \
             patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                        "google.cloud.firestore": mock_fs}):
            resp = client.post(
                f"/api/runs/{_make_run_id()}/zones/0/review",
                json={"review_status": "APPROVED", "review_comment": "re-confirm"}
            )

        assert resp.status_code == 200
        # approved stays 1, pending stays 0
        counts = counts_written.get("review_counts", {})
        assert counts.get("approved") == 1
        assert counts.get("pending") == 0

    def test_rejected_makes_overall_rejected(self, client):
        """If any zone rejected, overall = REJECTED (§7.2)."""
        mock_db = MagicMock()
        counts_written = {}

        header_snap = MagicMock()
        header_snap.exists = True
        header_snap.to_dict.return_value = {
            "review_counts": {"pending": 2, "approved": 1, "rejected": 0}
        }

        zone_snap = MagicMock()
        zone_snap.exists = True
        zone_snap.to_dict.return_value = {"review": {"review_status": "PENDING"}}

        header_ref = MagicMock()
        header_ref.get.return_value = header_snap
        zone_ref = MagicMock()
        zone_ref.get.return_value = zone_snap

        txn = MagicMock()
        txn.update.side_effect = lambda ref, data: counts_written.update(data) if "review_counts" in data else None

        def collection_side(name):
            col = MagicMock()
            if name == "template_runs":
                col.document.return_value = header_ref
            else:
                col.document.return_value = zone_ref
            return col

        mock_db.collection.side_effect = collection_side
        mock_db.transaction.return_value = txn

        mock_fs = MagicMock()
        mock_fs.transactional = lambda fn: (lambda t: fn(t))
        mock_fs.SERVER_TIMESTAMP = "ts"

        from app.pipeline import history_routes
        with patch.object(history_routes, "FIRESTORE_AVAILABLE", True), \
             patch.object(history_routes, "get_db", return_value=mock_db), \
             patch.dict("sys.modules", {"google.cloud": MagicMock(firestore=mock_fs),
                                        "google.cloud.firestore": mock_fs}):
            resp = client.post(
                f"/api/runs/{_make_run_id()}/zones/1/review",
                json={"review_status": "REJECTED"}
            )

        assert resp.status_code == 200
        assert counts_written.get("review_overall_status") == "REJECTED"


# ---------------------------------------------------------------------------
# /run endpoint persistence flags integration test
# ---------------------------------------------------------------------------

class TestRunEndpointPersistenceFlags:
    """
    Tests that POST /api/templates/{name}/run always returns persistence flags.
    Uses mocked template_store + mocked persist_run.
    """

    @pytest.fixture
    def run_client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.pipeline.run_routes import run_router

        test_app = FastAPI()
        test_app.include_router(run_router)
        return TestClient(test_app)

    def _make_template(self):
        from app.pipeline.models import TemplateDef, ZoneDef
        return TemplateDef(
            template_name="test_tmpl",
            schema_version=1,
            source_size=[100, 100],
            zones=[
                ZoneDef(name="headline", type="ocr", bbox=[0, 0, 50, 50], engines=["google"])
            ],
        )

    def _minimal_image_bytes(self):
        # 1x1 white PNG
        return (
            b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
            b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00'
            b'\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
        )

    def test_persistence_flags_present_on_success(self, run_client):
        from app.pipeline import run_routes, template_store, persistence

        tmpl = self._make_template()

        with patch.object(template_store, "get_template", return_value=tmpl), \
             patch("app.pipeline.run_routes.dispatch_zone_ocr", return_value=[]), \
             patch("app.pipeline.run_routes.resolve_consensus",
                   return_value={"zone_status": "OK", "selected_text": "", "zone_name": "headline"}), \
             patch("app.pipeline.run_routes.build_validation_result", return_value=({}, None)), \
             patch("app.pipeline.run_routes.persist_run",
                   return_value={"persisted": True, "persistence_error": False,
                                 "persistence_error_type": None}):
            resp = run_client.post(
                "/api/templates/test_tmpl/run",
                files={"image": ("test.png", self._minimal_image_bytes(), "image/png")},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["persisted"] is True
        assert body["persistence_error"] is False
        assert body["persistence_error_type"] is None

    def test_persistence_failure_still_returns_200(self, run_client):
        from app.pipeline import run_routes, template_store

        tmpl = self._make_template()

        with patch.object(template_store, "get_template", return_value=tmpl), \
             patch("app.pipeline.run_routes.dispatch_zone_ocr", return_value=[]), \
             patch("app.pipeline.run_routes.resolve_consensus",
                   return_value={"zone_status": "OK", "selected_text": "", "zone_name": "headline"}), \
             patch("app.pipeline.run_routes.build_validation_result", return_value=({}, None)), \
             patch("app.pipeline.run_routes.persist_run",
                   return_value={"persisted": False, "persistence_error": True,
                                 "persistence_error_type": "firestore_write_failed"}):
            resp = run_client.post(
                "/api/templates/test_tmpl/run",
                files={"image": ("test.png", self._minimal_image_bytes(), "image/png")},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["persisted"] is False
        assert body["persistence_error"] is True
        assert body["persistence_error_type"] == "firestore_write_failed"
        # Computed zones still returned
        assert "zones" in body
