import os
from types import SimpleNamespace

os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app import main
from app.pipeline import phase2_routes


def test_resolve_target_bboxes_reads_phase2_target_meta(monkeypatch):
    zones = [
        SimpleNamespace(type="ocr", bbox=[1, 2, 30, 40], notes='{"phase2_target_id":"700"}'),
        SimpleNamespace(type="logo", bbox=[0, 0, 10, 10], notes='{"phase2_target_id":"1080"}'),
        SimpleNamespace(type="ocr", bbox=[2, 3, 40, 60], notes='{"phase2_target_id":"700"}'),
    ]
    fake_template = SimpleNamespace(zones=zones)

    monkeypatch.setattr(main.template_store, "get_template", lambda _name: fake_template)

    out = phase2_routes._resolve_target_bboxes("archive.zip")
    assert out == {"700": [1, 2, 30, 40]}
