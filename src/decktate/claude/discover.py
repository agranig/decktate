"""Which Claude Code sessions are running on this computer.

Claude Code keeps one small file per running process in ~/.claude/sessions/<pid>.json
(session id, folder, name, idle/busy). That is not a documented interface, so everything
here is best effort: a file that cannot be read is skipped, never an error.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

# the variables that tell which terminal (and which tab of it) a session runs in
TERMINAL_VARS = ("TERM_PROGRAM", "ITERM_SESSION_ID", "TERM_SESSION_ID", "TMUX", "TMUX_PANE", "KITTY_WINDOW_ID",
                 "KITTY_LISTEN_ON", "WEZTERM_PANE")


@dataclass
class Running:
    id: str  # session id
    pid: int
    cwd: str = ""
    title: str = ""  # a name the user gave the session; empty for names Claude Code made up
    busy: bool = False
    started: float = 0.0


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, not ours


def running(base: Path | None = None, alive=alive) -> list[Running]:
    """Interactive Claude Code sessions whose process is alive, oldest first."""
    base = base or Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "sessions"
    found = []
    try:
        files = list(base.glob("*.json"))
    except OSError:
        return []
    for f in files:
        try:
            d = json.loads(f.read_text())
            pid, sid = int(d["pid"]), str(d["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if d.get("kind", "interactive") != "interactive" or not alive(pid):
            continue
        named = d.get("name") if d.get("nameSource") not in (None, "derived", "auto") else ""
        found.append(Running(sid, pid, str(d.get("cwd") or ""), str(named or ""), d.get("status") == "busy",
                             float(d.get("startedAt") or 0) / 1000))
    return sorted(found, key=lambda r: r.started)


def terminal_env(pid: int) -> dict:
    """The terminal variables of a running session, read from its process (same user only).
    Needed to type into a session that has not sent a hook yet. Nothing else of the environment is kept."""
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    env = {}
    for word in out.split():
        name, eq, value = word.partition("=")
        if eq and name in TERMINAL_VARS and value:
            env[name] = value
    return env


def quit(pid: int) -> bool:
    """Ask a session to exit (the same as closing it; the conversation can be resumed later)."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False
