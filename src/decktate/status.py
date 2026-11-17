"""Status colours shared by the state machine and the renderer."""

from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    RUNNING = "running"
    UNKNOWN = "unknown"
    IDLE = "idle"
