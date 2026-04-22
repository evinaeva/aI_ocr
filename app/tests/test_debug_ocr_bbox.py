import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app import main


class _Upload:
    def __init__(self, payload: bytes):
        self._payload = payload

    async def read(self):
        return self._payload


def test_debug_ocr_returns_missing_bbox_only_when_absent(monkeypatch):
    monkeypatch.setattr(
        main,
        "process_zip",
        lambda _zip: SimpleNamespace(images={"en": b"img"}, image_names={"en": "images/a.png"}, texts={}),
    )
    manifest = [SimpleNamespace(items=[SimpleNamespace(archive_path="images/a.png", lang="en")])]
    monkeypatch.setattr(main, "build_zip_manifest", lambda _zip: manifest)
    monkeypatch.setattr(
        main,
        "_run_zone_ocr_for_engines",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("OCR must not run without bbox")),
    )

    response = asyncio.run(main.debug_ocr(_Upload(b"zip")))
    payload = response.body.decode("utf-8")

    assert '"reason":"missing_bbox"' in payload


def test_debug_ocr_uses_tuple_bbox_and_runs_ocr(monkeypatch):
    monkeypatch.setattr(
        main,
        "process_zip",
        lambda _zip: SimpleNamespace(images={"en": b"img"}, image_names={"en": "images/a.png"}, texts={}),
    )
    manifest = [SimpleNamespace(items=[SimpleNamespace(archive_path="images/a.png", lang="en", bbox=(1, 2, 30, 40))])]
    monkeypatch.setattr(main, "build_zip_manifest", lambda _zip: manifest)
    monkeypatch.setattr(main, "_crop_zip_zone", lambda *_args, **_kwargs: "crop")
    monkeypatch.setattr(main, "_run_zone_ocr_for_engines", lambda *_args, **_kwargs: {"google": SimpleNamespace(text="ok", confidence=0.9)})

    response = asyncio.run(main.debug_ocr(_Upload(b"zip")))
    payload = response.body.decode("utf-8")

    assert '"reason":null' in payload
    assert '"google"' in payload
