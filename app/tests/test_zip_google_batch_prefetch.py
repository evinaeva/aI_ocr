import os
os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app import main
from app.ocr import OCRResult


class _Item:
    def __init__(self, archive_path: str):
        self.archive_path = archive_path


def test_prefetch_google_for_zip_items_batches_and_populates_cache(monkeypatch):
    queue_items = [_Item(f"images/{i}.png") for i in range(17)]

    bytes_by_path = {item.archive_path: f"img-{idx}".encode() for idx, item in enumerate(queue_items)}

    monkeypatch.setattr(main, "_read_archive_image", lambda _zip, p: bytes_by_path[p])

    captured = {"batch_sizes": [], "cache_put_ids": []}

    def _fake_batch(images, google_mode=None):
        captured["batch_sizes"].append(len(images))
        return [OCRResult(f"txt-{i}", 0.9, "google") for i, _ in enumerate(images)]

    def _fake_cache_put(image_bytes, result):
        captured["cache_put_ids"].append(id(image_bytes))
        assert isinstance(result, OCRResult)

    monkeypatch.setattr(main, "google_batch_annotate_images", _fake_batch)
    monkeypatch.setattr(main, "_google_cache_put", _fake_cache_put)

    preloaded, failed, cache_ids = main._prefetch_google_for_zip_items(queue_items, b"zip", ["google", "azure"])

    assert failed == set()
    assert len(preloaded) == 17
    assert captured["batch_sizes"] == [17]
    assert len(captured["cache_put_ids"]) == 17
    assert cache_ids == captured["cache_put_ids"]


def test_prefetch_google_for_zip_items_no_single_google_fallback_on_batch_error(monkeypatch):
    queue_items = [_Item("images/ok.png")]
    img_bytes = b"ok"

    monkeypatch.setattr(main, "_read_archive_image", lambda _zip, _path: img_bytes)
    monkeypatch.setattr(main, "google_batch_annotate_images", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    clear_calls = []
    monkeypatch.setattr(main, "_google_cache_clear", lambda ids: clear_calls.append(list(ids)))

    preloaded, failed, cache_ids = main._prefetch_google_for_zip_items(queue_items, b"zip", ["google"])

    assert failed == set()
    assert preloaded == {0: img_bytes}
    assert cache_ids == []
    assert clear_calls == []
