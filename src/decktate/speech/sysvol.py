"""The computer's own output volume (what the volume keys on the keyboard change).

macOS: AppleScript `set volume`. Linux: PipeWire (`wpctl`), PulseAudio (`pactl`)
or ALSA (`amixer`), whichever is installed. It always acts on the system's
default output device.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys


def _run(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def get() -> int | None:
    """System output volume in percent, or None if it cannot be read."""
    out = None
    if sys.platform == "darwin":
        out = _run(["osascript", "-e", "output volume of (get volume settings)"])
        return int(out.strip()) if out and out.strip().isdigit() else None
    if shutil.which("wpctl"):
        out = _run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
        m = re.search(r"([\d.]+)", out or "")
        return round(float(m.group(1)) * 100) if m else None
    if shutil.which("pactl"):
        out = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    elif shutil.which("amixer"):
        out = _run(["amixer", "get", "Master"])
    m = re.search(r"(\d+)%", out or "")
    return int(m.group(1)) if m else None


def set(percent: int) -> bool:
    """Set the system output volume (0-100). Returns False if no tool for it was found."""
    percent = max(0, min(100, int(percent)))
    if sys.platform == "darwin":
        return _run(["osascript", "-e", f"set volume output volume {percent}"]) is not None
    if shutil.which("wpctl"):
        return _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent / 100:.2f}"]) is not None
    if shutil.which("pactl"):
        return _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"]) is not None
    if shutil.which("amixer"):
        return _run(["amixer", "set", "Master", f"{percent}%"]) is not None
    return False
