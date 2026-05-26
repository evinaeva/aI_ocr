"""Build a per-banner ok/flag report from block-level compare results."""
from __future__ import annotations

from dataclasses import dataclass, field

from .compare import CompareResult
from .detect import BBox


@dataclass
class BlockReport:
    bbox: BBox
    reference_text: str
    compare: CompareResult

    @property
    def flagged(self) -> bool:
        return self.compare.flagged


@dataclass
class BannerReport:
    banner_path: str
    language: str
    blocks: list[BlockReport] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return any(b.flagged for b in self.blocks)

    @property
    def status(self) -> str:
        return "flag" if self.flagged else "ok"

    def render(self) -> str:
        lines = [f"{self.banner_path}  [{self.language}]  {self.status.upper()}"]
        for b in self.blocks:
            lines.append(f"  {b.compare}  text={b.reference_text!r}")
        return "\n".join(lines)
