"""Install / remove Decktate's hooks and status line in Claude Code's settings.

Only our own entries are touched. They are recognised by the hook URL, so the
user's other hooks stay as they are, and uninstall restores the status line
command that was there before.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path

from ..config import Config
from ..core.server import ENV_HEADERS

EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
          "PermissionRequest", "PermissionDenied", "Notification", "SubagentStart", "SubagentStop",
          "PreCompact", "Stop", "StopFailure", "SessionEnd"]
HELD = {"PermissionRequest", "PreToolUse"}  # these may wait for a key press
MARK = "/hook"  # our hooks post to http://127.0.0.1:<port>/hook


def settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "settings.json"


def _url(cfg: Config) -> str:
    return f"http://127.0.0.1:{cfg.port}{MARK}"


def _is_ours(hook: dict) -> bool:
    return hook.get("type") == "http" and "127.0.0.1" in hook.get("url", "") and hook.get("url", "").endswith(MARK)


def hook_entry(cfg: Config, event: str) -> dict:
    headers = {"X-Decktate-Token": cfg.token()}
    headers.update({"-".join(p.capitalize() for p in h.split("-")): f"${{{name}}}" for h, name in ENV_HEADERS.items()
                    if name != "TTY"})
    return {
        "type": "http", "url": _url(cfg), "headers": headers,
        "allowedEnvVars": sorted(n for n in ENV_HEADERS.values() if n != "TTY"),
        # held hooks wait for a key; the rest must never slow Claude down
        "timeout": int(cfg.permission_hold_s) + 15 if event in HELD else 5,
    }


def statusline_script(cfg: Config, previous: str) -> str:
    """A tiny shell script: forward the status JSON to the daemon in the
    background, then run whatever status line was configured before."""
    prev = previous or "printf ''"
    return f"""#!/bin/sh
# Decktate status line forwarder. Generated; `decktate uninstall` removes it.
input=$(cat)
printf '%s' "$input" | curl -s -m 1 -X POST -H 'Content-Type: application/json' \\
  -H 'X-Decktate-Token: {cfg.token()}' --data-binary @- http://127.0.0.1:{cfg.port}/statusline >/dev/null 2>&1 &
printf '%s' "$input" | {prev}
"""


def _write(path: Path, settings: dict) -> None:
    """Replace the file in one step, so Claude Code never reads a half-written settings file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".decktate-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, path)


def install(cfg: Config, path: Path | None = None, statusline: bool = True, backup: str = "dated") -> dict:
    """Merge our hooks into settings.json. Returns a summary of what changed.
    backup: "dated" = a new timestamped copy (manual install); "once" = keep a single
    copy of the file as it was before Decktate first touched it (automatic install)."""
    path = path or settings_path()
    settings = json.loads(path.read_text()) if path.exists() else {}
    saved = None
    if path.exists():
        if backup == "dated":
            saved = path.with_name(f"{path.name}.decktate-backup-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, saved)
        elif not installed(cfg, path)["events"]:  # only a file without our hooks is worth keeping
            saved = path.with_name(f"{path.name}.decktate-backup")
            shutil.copy2(path, saved)
    backup = saved
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        groups = [g for g in hooks.get(event, []) if not any(_is_ours(h) for h in g.get("hooks", []))]
        groups.append({"hooks": [hook_entry(cfg, event)]})
        hooks[event] = groups
    result = {"settings": str(path), "backup": str(backup) if backup else "", "events": len(EVENTS), "statusline": ""}
    if statusline:
        script = cfg.home / "statusline.sh"
        current = (settings.get("statusLine") or {}).get("command", "")
        state = cfg.home / "statusline.previous.json"
        if str(script) not in current:  # remember what was there, once
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"statusLine": settings.get("statusLine")}))
            previous = current
        else:
            previous = (json.loads(state.read_text()).get("statusLine") or {}).get("command", "") if state.exists() else ""
        script.write_text(statusline_script(cfg, previous))
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        keep = {k: v for k, v in (settings.get("statusLine") or {}).items() if k not in ("type", "command")}
        settings["statusLine"] = {**keep, "type": "command", "command": str(script)}
        result["statusline"] = "chained after: " + previous if previous else "installed"
    _write(path, settings)
    return result


def uninstall(cfg: Config, path: Path | None = None) -> dict:
    path = path or settings_path()
    if not path.exists():
        return {"settings": str(path), "removed": 0}
    settings = json.loads(path.read_text())
    removed = 0
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        kept = []
        for group in hooks[event]:
            mine = [h for h in group.get("hooks", []) if _is_ours(h)]
            removed += len(mine)
            rest = [h for h in group.get("hooks", []) if not _is_ours(h)]
            if rest:
                kept.append({**group, "hooks": rest})
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    script, state = cfg.home / "statusline.sh", cfg.home / "statusline.previous.json"
    if str(script) in (settings.get("statusLine") or {}).get("command", ""):
        previous = json.loads(state.read_text()).get("statusLine") if state.exists() else None
        if previous:
            settings["statusLine"] = previous
        else:
            settings.pop("statusLine", None)
    for f in (script, state):
        f.unlink(missing_ok=True)
    _write(path, settings)
    return {"settings": str(path), "removed": removed}


class AutoHooks:
    """Hooks that exist only while Decktate runs: added on start, removed on shutdown.

    If Decktate is killed without a chance to clean up, the hooks stay behind.
    That is harmless (Claude Code ignores a hook endpoint that is not there) and
    the next start replaces them.
    """

    def __init__(self, cfg: Config, path: Path | None = None):
        self.cfg, self.path, self.active = cfg, path, False

    def start(self) -> str:
        """Returns '' on success, else why the hooks could not be installed."""
        if not self.cfg.auto_hooks:
            return ""
        try:
            install(self.cfg, self.path, backup="once")
            self.active = True
            return ""
        except Exception as exc:  # e.g. settings.json is not valid JSON: leave it alone
            return f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        try:
            uninstall(self.cfg, self.path)
        except Exception:
            pass


def installed(cfg: Config, path: Path | None = None) -> dict:
    path = path or settings_path()
    settings = json.loads(path.read_text()) if path.exists() else {}
    events = [e for e, groups in (settings.get("hooks") or {}).items()
              if any(_is_ours(h) for g in groups for h in g.get("hooks", []))]
    urls = {h.get("url") for groups in (settings.get("hooks") or {}).values() for g in groups for h in g.get("hooks", [])
            if _is_ours(h)}
    return {"events": sorted(events), "complete": set(events) >= set(EVENTS),
            "current": urls == {_url(cfg)},  # False when the hooks still point at an old port
            "statusline": str(cfg.home / "statusline.sh") in (settings.get("statusLine") or {}).get("command", "")}
