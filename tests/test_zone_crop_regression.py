from __future__ import annotations

import io
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from fastapi import FastAPI
from app.pipeline.run_routes import run_router
from app.ocr import OCRResult
from app.pipeline.models import TemplateDef, ZoneDef


def _synthetic_banner() -> bytes:
    img = Image.new("RGB", (800, 400), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((30, 20), "OUTSIDE_TEXT", fill=(0, 0, 0))
    draw.text((120, 170), "INSIDE_TEXT", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_run_route_sends_zone_crop_to_engine():
    banner = _synthetic_banner()
    template = TemplateDef(
        template_name="crop_regression",
        source_size=[800, 400],
        zones=[
            ZoneDef(
                name="main_text",
                type="ocr",
                bbox=[100, 150, 320, 260],
                engines=["azure"],
                engine_config={},
            )
        ],
        expected_texts={},
    )

    payload_dims = []

    def fake_run_ocr_multi(image_bytes: bytes, engines, _engine_config):
        with Image.open(io.BytesIO(image_bytes)) as payload:
            payload_dims.append(payload.size)
            text = "INSIDE_TEXT" if payload.width < 700 else "OUTSIDE_TEXT"
        return {engines[0]: OCRResult(text=text, confidence=0.99, engine=engines[0])}

    with patch("app.pipeline.run_routes.template_store.get_template", return_value=template), \
         patch("app.pipeline.ocr_dispatcher.run_ocr_multi", side_effect=fake_run_ocr_multi), \
         patch("app.pipeline.run_routes.is_persistence_enabled", return_value=False):
        test_app = FastAPI()
        test_app.include_router(run_router)
        client = TestClient(test_app)
        response = client.post(
            "/api/templates/crop_regression/run",
            files={"image": ("banner.png", io.BytesIO(banner), "image/png")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["zones"][0]["consensus"]["selected_text"] == "INSIDE_TEXT"

    assert len(payload_dims) == 1
    crop_w, crop_h = payload_dims[0]
    assert crop_w < 700
    assert crop_h < 380
