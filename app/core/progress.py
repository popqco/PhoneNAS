from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SyncProgress:
    stage: str
    message: str
    bytes_done: int | None = None
    bytes_total: int | None = None
    items_done: int | None = None
    items_total: int | None = None

    @property
    def percent(self) -> int | None:
        if (self.stage not in ("staging", "applying")
                or type(self.bytes_done) is not int or self.bytes_done < 0
                or type(self.bytes_total) is not int or self.bytes_total <= 0):
            return None
        return min(100, self.bytes_done * 100 // self.bytes_total)

    def display(self, elapsed_seconds: float = 0) -> str:
        parts = [self.message]
        if self.percent is not None:
            parts.extend((f"本阶段 {self.percent}%",
                          f"{self.bytes_done / 1048576:.1f} / {self.bytes_total / 1048576:.1f} MiB"))
        if (type(self.items_done) is int and self.items_done >= 0
                and type(self.items_total) is int and self.items_total >= self.items_done):
            parts.append(f"{self.items_done} / {self.items_total} 项")
        if (type(elapsed_seconds) in (int, float) and math.isfinite(elapsed_seconds)
                and elapsed_seconds >= 1):
            seconds = int(elapsed_seconds)
            parts.append(f"已用时 {seconds // 60:02d}:{seconds % 60:02d}")
        return " · ".join(parts)
