"""
Phase 6 Router Registration Fix — Tests (Variant A)

Test 1: OpenAPI exposes Phase 6 endpoints even when persistence is disabled.
Test 2: Phase 6 endpoints return 503 (not 404) when persistence is disabled.
Test 3: /run returns deterministic disabled flags when persistence is disabled.

All tests run without google-cloud-firestore installed and without real Firestore.
"""
from __future__ import annotations

import io
import os
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_png_bytes(width: int = 10, height: int = 10) -> bytes:
    """Return minimal valid PNG bytes."""
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _minimal_template_mock():
    """Return a mock TemplateDef-like object with no zones."""
    tmpl = MagicMock()
    tmpl.zones = []
    tmpl.source_size = [100, 100]
    tmpl.expected_texts = {}
    return tmpl


# ---------------------------------------------------------------------------
# Fixture: app client with persistence DISABLED
# ---------------------------------------------------------------------------

@pytest.fixture()
def disabled_client(monkeypatch):
    """
    Provide a TestClient where FIRESTORE_AVAILABLE env var is 'false',
    ensuring PERSISTENCE_ENABLED evaluates to False.
    """
    monkeypatch.setenv("FIRESTORE_AVAILABLE", "false")

    # Reload firestore_store so is_persistence_enabled() re-reads env
    import importlib
    import app.pipeline.firestore_store as fs_mod
    importlib.reload(fs_mod)

    # Patch is_persistence_enabled globally so all callers see False
    with patch("app.pipeline.firestore_store.is_persistence_enabled", return_value=False), \
         patch("app.pipeline.history_routes.is_persistence_enabled", return_value=False), \
         patch("app.pipeline.run_routes.is_persistence_enabled", return_value=False):

        # Import app AFTER patching
        import app.main as main_mod
        importlib.reload(main_mod)
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        yield client


# ---------------------------------------------------------------------------
# Test 1: OpenAPI exposes Phase 6 endpoints when persistence is disabled
# ---------------------------------------------------------------------------

class TestOpenAPIEndpointsExist:
    REQUIRED_PATHS = [
        "/api/templates/{template_name}/history",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/zones/{zone_index}/review",
    ]

    def test_openapi_paths_present(self, disabled_client):
        response = disabled_client.get("/openapi.json")
        assert response.status_code == 200
        openapi = response.json()
        paths = openapi.get("paths", {})
        for path in self.REQUIRED_PATHS:
            assert path in paths, (
                f"Expected path '{path}' in /openapi.json but it was missing. "
                f"Available paths: {list(paths.keys())}"
            )


# ---------------------------------------------------------------------------
# Test 2: Phase 6 endpoints return 503 when persistence disabled
# ---------------------------------------------------------------------------

class TestEndpointsReturn503WhenDisabled:
    _DISABLED_BODY = {"detail": "Persistence disabled"}

    def test_history_returns_503(self, disabled_client):
        response = disabled_client.get("/api/templates/x/history")
        assert response.status_code == 503, (
            f"Expected 503, got {response.status_code}: {response.text}"
        )
        assert response.json() == self._DISABLED_BODY

    def test_get_run_returns_503(self, disabled_client):
        response = disabled_client.get("/api/runs/some-id")
        assert response.status_code == 503, (
            f"Expected 503, got {response.status_code}: {response.text}"
        )
        assert response.json() == self._DISABLED_BODY

    def test_review_returns_503(self, disabled_client):
        response = disabled_client.post(
            "/api/runs/some-id/zones/0/review",
            json={"review_status": "APPROVED"},
        )
        assert response.status_code == 503, (
            f"Expected 503, got {response.status_code}: {response.text}"
        )
        assert response.json() == self._DISABLED_BODY


# ---------------------------------------------------------------------------
# Test 3: /run returns deterministic disabled flags
# ---------------------------------------------------------------------------

class TestRunReturnsDeterministicDisabledFlags:

    def test_run_disabled_persistence_flags(self, disabled_client, monkeypatch):
        """
        With persistence disabled and OCR mocked, POST /run must:
        - return 200
        - include persisted=false, persistence_error=false, persistence_error_type=null
        """
        template_name = "test_tmpl"
        png_bytes = _make_png_bytes()

        # Mock template_store.get_template to return a minimal template
        with patch("app.pipeline.run_routes.template_store") as mock_ts, \
             patch("app.pipeline.run_routes.dispatch_zone_ocr", return_value=[]):

            mock_ts.get_template.return_value = _minimal_template_mock()

            response = disabled_client.post(
                f"/api/templates/{template_name}/run",
                files={"image": ("test.png", io.BytesIO(png_bytes), "image/png")},
            )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        body = response.json()
        assert body.get("persisted") is False, f"Expected persisted=false, got: {body}"
        assert body.get("persistence_error") is False, (
            f"Expected persistence_error=false, got: {body}"
        )
        assert body.get("persistence_error_type") is None, (
            f"Expected persistence_error_type=null, got: {body}"
        )
        # Minimal run output keys
        assert "run_id" in body
        assert "template_name" in body
        assert "zones" in body
