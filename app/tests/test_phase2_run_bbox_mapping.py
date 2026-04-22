import os
from types import SimpleNamespace

os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app import main
from app.pipeline import phase2_routes


def test_resolve_target_zones_reads_all_phase2_target_meta(monkeypatch):
    zones = [
        SimpleNamespace(type="ocr", name="title", bbox=[1, 2, 30, 40], notes='{"phase2_target_id":"700","phase2_zone_name":"title"}'),
        SimpleNamespace(type="logo", bbox=[0, 0, 10, 10], notes='{"phase2_target_id":"1080"}'),
        SimpleNamespace(type="ocr", name="cta", bbox=[2, 3, 40, 60], notes='{"phase2_target_id":"700","phase2_zone_name":"cta"}'),
    ]
    fake_template = SimpleNamespace(zones=zones, expected_texts={"en": {"title": "Hello", "cta": "Buy now"}})

    monkeypatch.setattr(main.template_store, "get_template", lambda _name: fake_template)

    out = phase2_routes._resolve_target_zones("archive.zip")
    assert list(out.keys()) == ["700"]
    assert len(out["700"]) == 2
    assert out["700"][0]["bbox"] == [1, 2, 30, 40]
    assert out["700"][0]["zone_name"] == "title"
    assert out["700"][0]["expected_by_lang"]["en"] == "Hello"
    assert out["700"][1]["bbox"] == [2, 3, 40, 60]
    assert out["700"][1]["zone_name"] == "cta"
