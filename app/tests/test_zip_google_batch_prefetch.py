import io
import struct
import zlib
import os
os.environ.setdefault("APP_PASSWORD", "test")
os.environ.setdefault("SESSION_SECRET", "testsecret")

from app import main
from app.ocr import OCRResult


def _png_bytes(width: int = 20, height: int = 20, color=(3, 4, 5)) -> bytes:
    r, g, b = color
    row = b"\x00" + bytes([r, g, b]) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")



class _Item:
    def __init__(self, archive_path: str):
        self.archive_path = archive_path


def test_prefetch_google_for_zip_items_batches_and_populates_cache(monkeypatch):
    queue_items = [_Item(f"images/{i}.png") for i in range(17)]

    bytes_by_path = {item.archive_path: f"img-{idx}".encode() for idx, item in enumerate(queue_items)}

    monkeypatch.setattr(main, "_read_archive_image", lambda _zip, p: bytes_by_path[p])
    queue_items[0].bbox = [1, 1, 10, 10]
    for item in queue_items[1:]:
        item.bbox = [1, 1, 10, 10]
    monkeypatch.setattr(
        main,
        "_crop_zip_zone",
        lambda b, bbox: main.make_cropped_image(
            b,
            bbox,
            _png_bytes(20, 20),
            original_width=100,
            original_height=100,
            crop_width=20,
            crop_height=20,
        ),
    )

    captured = {"batch_sizes": [], "cache_put_ids": []}

    def _fake_batch(images, google_mode=None):
        captured["batch_sizes"].append(len(images))
        return [OCRResult(f"txt-{i}", 0.9, "google") for i, _ in enumerate(images)]

    def _fake_cache_put(image_bytes, result):
        captured["cache_put_ids"].append(id(image_bytes))
        assert isinstance(result, OCRResult)

    monkeypatch.setattr(main, "google_batch_annotate_images", _fake_batch)
    monkeypatch.setattr(main, "_google_cache_put", _fake_cache_put)

    preloaded, cropped, failed, missing_bbox, crop_required, cache_ids = main._prefetch_google_for_zip_items(queue_items, b"zip", ["google", "azure"])

    assert failed == set()
    assert missing_bbox == set()
    assert crop_required == set()
    assert len(preloaded) == 17
    assert len(cropped) == 17
    assert captured["batch_sizes"] == [17]
    assert len(captured["cache_put_ids"]) == 17
    assert cache_ids == captured["cache_put_ids"]


def test_prefetch_google_for_zip_items_no_single_google_fallback_on_batch_error(monkeypatch):
    queue_items = [_Item("images/ok.png")]
    img_bytes = b"ok"

    monkeypatch.setattr(main, "_read_archive_image", lambda _zip, _path: img_bytes)
    queue_items[0].bbox = [1, 1, 10, 10]
    monkeypatch.setattr(
        main,
        "_crop_zip_zone",
        lambda b, bbox: main.make_cropped_image(
            b,
            bbox,
            _png_bytes(20, 20),
            original_width=100,
            original_height=100,
            crop_width=20,
            crop_height=20,
        ),
    )
    monkeypatch.setattr(main, "google_batch_annotate_images", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    clear_calls = []
    monkeypatch.setattr(main, "_google_cache_clear", lambda ids: clear_calls.append(list(ids)))

    preloaded, cropped, failed, missing_bbox, crop_required, cache_ids = main._prefetch_google_for_zip_items(queue_items, b"zip", ["google"])

    assert failed == set()
    assert missing_bbox == set()
    assert crop_required == set()
    assert preloaded == {0: img_bytes}
    assert 0 in cropped
    assert cache_ids == []
    assert clear_calls == []


def test_prefetch_google_for_zip_items_marks_missing_bbox(monkeypatch):
    queue_items = [_Item("images/ok.png")]
    img_bytes = b"ok"

    monkeypatch.setattr(main, "_read_archive_image", lambda _zip, _path: img_bytes)

    preloaded, cropped, failed, missing_bbox, crop_required, cache_ids = main._prefetch_google_for_zip_items(queue_items, b"zip", ["google"])

    assert failed == set()
    assert preloaded == {0: img_bytes}
    assert cropped == {}
    assert missing_bbox == {0}
    assert crop_required == set()
    assert cache_ids == []


def test_prefetch_google_for_zip_items_accepts_tuple_bbox(monkeypatch):
    queue_items = [_Item("images/ok.png")]
    queue_items[0].bbox = (1, 1, 10, 10)
    img_bytes = b"ok"

    monkeypatch.setattr(main, "_read_archive_image", lambda _zip, _path: img_bytes)
    monkeypatch.setattr(
        main,
        "_crop_zip_zone",
        lambda b, bbox: main.make_cropped_image(
            b,
            bbox,
            _png_bytes(20, 20),
            original_width=100,
            original_height=100,
            crop_width=20,
            crop_height=20,
        ),
    )

    preloaded, cropped, failed, missing_bbox, crop_required, cache_ids = main._prefetch_google_for_zip_items(queue_items, b"zip", ["google"])

    assert failed == set()
    assert preloaded == {0: img_bytes}
    assert 0 in cropped
    assert missing_bbox == set()
    assert crop_required == set()
    assert cache_ids == []
