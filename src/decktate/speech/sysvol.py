"""The Mac's own output volume (what the volume keys on the keyboard change), through AppleScript.
It always acts on the system's default output device."""

from __future__ import annotations

import subprocess


def _run(script: str) -> str | None:
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def get() -> int | None:
    """System output volume in percent, or None if it cannot be read."""
    out = _run("output volume of (get volume settings)")
    return int(out.strip()) if out and out.strip().isdigit() else None


def set(percent: int) -> bool:
    """Set the system output volume (0-100)."""
    return _run(f"set volume output volume {max(0, min(100, int(percent)))}") is not None
