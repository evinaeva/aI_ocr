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


# Tests for `_process_session` without a template/target_zones were removed
# along with the whole-image OCR legacy code path — every session must now
# be driven by a template's crop layout (see `_start_session_from_zip`).
