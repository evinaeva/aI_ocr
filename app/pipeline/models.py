""" 
Canonical data models for the pipeline (schema_version=1).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, field_validator, model_validator

ALLOWED_ENGINES = {"google", "azure", "ocrspace"}


class ZoneDef(BaseModel):
    name: str
    type: Literal["ocr", "logo"]
    bbox: List[int]
    engines: List[str]
    engine_config: Dict[str, Any] = {}
    notes: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name must be a non-empty string")
        return v

    @field_validator("bbox")
    @classmethod
    def bbox_valid(cls, v: List[int]) -> List[int]:
        if len(v) != 4:
            raise ValueError("bbox must be a list of 4 integers [x1, y1, x2, y2]")
        x1, y1, x2, y2 = v
        if x1 >= x2:
            raise ValueError("bbox x1 must be < x2")
        if y1 >= y2:
            raise ValueError("bbox y1 must be < y2")
        return v

    @field_validator("engines")
    @classmethod
    def engines_valid(cls, v: List[str]) -> List[str]:
        for e in v:
            if e not in ALLOWED_ENGINES:
                raise ValueError(f"engine '{e}' not allowed; must be one of {sorted(ALLOWED_ENGINES)}")
        return v

    @model_validator(mode="after")
    def logo_can_have_empty_engines(self) -> "ZoneDef":
        # type=ocr must have at least one engine; type=logo may have empty engines
        if self.type == "ocr" and len(self.engines) == 0:
            raise ValueError("zones with type='ocr' must have at least one engine")
        return self


class TemplateDef(BaseModel):
    template_name: str
    schema_version: Literal[1] = 1
    source_size: List[int]
    zones: List[ZoneDef] = []
    expected_texts: Dict[str, Any] = {}
    created_at_utc: Optional[str] = None
    updated_at_utc: Optional[str] = None

    @field_validator("template_name")
    @classmethod
    def name_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("template_name must be a non-empty string")
        return v

    @field_validator("source_size")
    @classmethod
    def source_size_valid(cls, v: List[int]) -> List[int]:
        if len(v) != 2:
            raise ValueError("source_size must be a list of exactly 2 integers [width, height]")
        if v[0] <= 0 or v[1] <= 0:
            raise ValueError("source_size dimensions must both be > 0")
        return v

    @model_validator(mode="after")
    def zone_names_unique(self) -> "TemplateDef":
        names = [z.name for z in self.zones]
        if len(names) != len(set(names)):
            raise ValueError("zone names must be unique within a template")
        return self

    def with_timestamps(self, *, update: bool = False) -> "TemplateDef":
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        data = self.model_dump()
        if not update or not data.get("created_at_utc"):
            data["created_at_utc"] = now
        data["updated_at_utc"] = now
        return TemplateDef(**data)
