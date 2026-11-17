"""Status colours shared by the state machine and the renderer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    RUNNING = "running"
    UNKNOWN = "unknown"
    IDLE = "idle"


@dataclass
class Series:
    """A row of numbers for a sparkline / chart."""

    key: str
    label: str
    points: list[float]
    unit: str = ""
    threshold: float | None = None
    higher_is_better: bool = True

    @property
    def last(self) -> float | None:
        return self.points[-1] if self.points else None

    @property
    def status(self) -> Status:
        if self.last is None:
            return Status.IDLE
        if self.threshold is None:
            return Status.OK
        good = self.last >= self.threshold if self.higher_is_better else self.last <= self.threshold
        return Status.OK if good else Status.FAIL
