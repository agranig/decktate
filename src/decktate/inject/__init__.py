"""Typing into a Claude Code session: prompts, slash commands, Esc, Shift+Tab.

Claude Code has no documented way to receive input from another process, so
this goes through the terminal the session runs in. Which terminal that is
comes from the session's own environment (sent along with every hook):

  iTerm2        scripting interface, addressed by session id; no focus needed
  Terminal.app  scripting interface for text (by tty), keystrokes for keys
  kitty         `kitty @ send-text` (needs allow_remote_control)
  WezTerm       `wezterm cli send-text`
  tmux          `tmux send-keys`, only if the session happens to run in tmux
  anything else keystrokes to the terminal app after bringing it to the front
                (needs the Accessibility permission)

Approvals, menu answers and "always allow" do NOT use any of this: they are
answered through hooks.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

KEYS = ("enter", "escape", "shift_tab", "ctrl_c", "ctrl_enter")
CTRL_ENTER = "\x1b[13;5u"  # Ctrl+Enter has no classic code; this is the "CSI u" form Claude Code asks terminals for
# TERM_PROGRAM -> macOS application name
MAC_APPS = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm2", "vscode": "Visual Studio Code", "ghostty": "Ghostty",
            "WarpTerminal": "Warp", "kitty": "kitty", "WezTerm": "WezTerm", "Hyper": "Hyper", "alacritty": "Alacritty",
            "zed": "Zed", "cursor": "Cursor"}
TYPE_PAUSE = 0.3  # Claude Code's input box swallows an Enter that arrives together with the text


class InjectError(RuntimeError):
    pass


def _run(cmd: list[str], input: str | None = None, timeout: float = 8) -> str:
    try:
        r = subprocess.run(cmd, input=input, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InjectError(f"{cmd[0]}: {type(exc).__name__}") from None
    if r.returncode != 0:
        raise InjectError((r.stderr or r.stdout or f"{cmd[0]} failed").strip().splitlines()[-1][:120])
    return r.stdout.strip()


def session_tty(session_id: str) -> str:
    """The tty of the Claude Code process of a session, e.g. /dev/ttys003.
    Best effort: uses ~/.claude/sessions/<pid>.json, which is not a documented interface."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / "sessions"
    try:
        for f in base.glob("*.json"):
            try:
                if json.loads(f.read_text()).get("sessionId") == session_id and f.stem.isdigit():
                    tty = _run(["ps", "-o", "tty=", "-p", f.stem])
                    return "/dev/" + tty if tty and tty not in ("?", "??") else ""
            except (ValueError, OSError, InjectError):
                continue
    except OSError:
        pass
    return ""


class Injector:
    name = "none"

    def send_text(self, text: str, enter: bool = True) -> None:
        raise InjectError("no way to type into this terminal")

    def key(self, key: str) -> None:
        raise InjectError("no way to type into this terminal")


class Tmux(Injector):
    name = "tmux"

    def __init__(self, pane: str):
        self.pane = pane

    def send_text(self, text, enter=True):
        _run(["tmux", "send-keys", "-t", self.pane, "-l", "--", text])
        if enter:
            time.sleep(TYPE_PAUSE)
            self.key("enter")

    def key(self, key):
        _run(["tmux", "send-keys", "-t", self.pane, {"enter": "Enter", "escape": "Escape", "shift_tab": "BTab",
                                                     "ctrl_c": "C-c", "ctrl_enter": "C-Enter"}[key]])


ITERM_SCRIPT = '''on run argv
  set wanted to item 1 of argv
  set payload to item 2 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (unique id of s) is wanted then
            tell s to write text payload newline NO
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  error "iTerm2 session not found"
end run'''


class ITerm(Injector):
    """iTerm2: writes straight into the session, wherever its window is."""

    name = "iTerm2"
    SEQ = {"enter": "\r", "escape": "\x1b", "shift_tab": "\x1b[Z", "ctrl_c": "\x03", "ctrl_enter": CTRL_ENTER}

    def __init__(self, session_id: str):
        self.sid = session_id.split(":")[-1]  # ITERM_SESSION_ID is "w0t0p0:<uuid>"

    def _write(self, payload: str) -> None:
        _run(["osascript", "-e", ITERM_SCRIPT, self.sid, payload])

    def send_text(self, text, enter=True):
        self._write(text)
        if enter:
            time.sleep(TYPE_PAUSE)
            self._write("\r")

    def key(self, key):
        self._write(self.SEQ[key])


KEYSTROKE_SCRIPT = '''on run argv
  set appName to item 1 of argv
  set what to item 2 of argv
  set payload to item 3 of argv
  set ttyName to item 4 of argv
  if appName is "Terminal" and ttyName is not "" then
    tell application "Terminal"
      repeat with w in windows
        repeat with t in tabs of w
          if (tty of t) is ttyName then
            set selected of t to true
            set index of w to 1
          end if
        end repeat
      end repeat
    end tell
  end if
  tell application appName to activate
  delay 0.25
  tell application "System Events"
    if what is "text" then
      keystroke payload
    else if what is "enter" then
      key code 36
    else if what is "escape" then
      key code 53
    else if what is "shift_tab" then
      key code 48 using shift down
    else if what is "ctrl_c" then
      keystroke "c" using control down
    else if what is "ctrl_enter" then
      key code 36 using control down
    end if
  end tell
  return "ok"
end run'''


class MacKeystrokes(Injector):
    """Any macOS terminal: bring it to the front and type. Needs the Accessibility
    permission for the app that runs Decktate, and it changes window focus."""

    def __init__(self, app: str, tty: str = ""):
        self.app, self.tty = app, tty
        self.name = app

    def _do(self, what: str, payload: str = "") -> None:
        try:
            _run(["osascript", "-e", KEYSTROKE_SCRIPT, self.app, what, payload, self.tty])
        except InjectError as exc:
            if "not allowed" in str(exc) or "1002" in str(exc) or "assistive" in str(exc):
                raise InjectError("needs Accessibility permission") from None
            raise

    def send_text(self, text, enter=True):
        self._do("text", text)
        if enter:
            time.sleep(TYPE_PAUSE)
            self._do("enter")

    def key(self, key):
        self._do(key)


TERMINAL_TEXT_SCRIPT = '''on run argv
  set ttyName to item 1 of argv
  set payload to item 2 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if (tty of t) is ttyName then
          do script payload in t
          return "ok"
        end if
      end repeat
    end repeat
  end tell
  error "Terminal tab not found"
end run'''


class AppleTerminal(MacKeystrokes):
    """Terminal.app: text goes through its scripting interface (no focus change);
    single keys have no scripting equivalent and use keystrokes."""

    def __init__(self, tty: str):
        super().__init__("Terminal", tty)
        self.name = "Terminal"

    def send_text(self, text, enter=True):
        if enter and self.tty:
            _run(["osascript", "-e", TERMINAL_TEXT_SCRIPT, self.tty, text])  # `do script` types the text and Return
        else:
            super().send_text(text, enter)


class Kitty(Injector):
    name = "kitty"
    SEQ = {"enter": "\r", "escape": "\x1b", "shift_tab": "\x1b[Z", "ctrl_c": "\x03", "ctrl_enter": CTRL_ENTER}

    def __init__(self, window: str, listen: str = ""):
        self.args = ["kitty", "@"] + (["--to", listen] if listen else []) + ["send-text", "--match", f"id:{window}", "--stdin"]

    def send_text(self, text, enter=True):
        _run(self.args, input=text)
        if enter:
            time.sleep(TYPE_PAUSE)
            self.key("enter")

    def key(self, key):
        _run(self.args, input=self.SEQ[key])


class WezTerm(Injector):
    name = "WezTerm"

    def __init__(self, pane: str):
        self.args = ["wezterm", "cli", "send-text", "--pane-id", pane, "--no-paste"]

    def send_text(self, text, enter=True):
        _run(self.args, input=text)
        if enter:
            time.sleep(TYPE_PAUSE)
            self.key("enter")

    def key(self, key):
        _run(self.args, input=Kitty.SEQ[key])


def for_session(session_id: str, terminal: dict, which=shutil.which, tty_of=session_tty) -> Injector:
    """Pick the best way to type into the terminal this session runs in."""
    term = terminal.get("TERM_PROGRAM", "")
    if terminal.get("TMUX_PANE") and which("tmux"):
        return Tmux(terminal["TMUX_PANE"])
    if terminal.get("KITTY_WINDOW_ID") and terminal.get("KITTY_LISTEN_ON") and which("kitty"):
        return Kitty(terminal["KITTY_WINDOW_ID"], terminal["KITTY_LISTEN_ON"])
    if terminal.get("WEZTERM_PANE") and which("wezterm"):
        return WezTerm(terminal["WEZTERM_PANE"])
    if terminal.get("ITERM_SESSION_ID"):
        return ITerm(terminal["ITERM_SESSION_ID"])
    if term == "Apple_Terminal":
        return AppleTerminal(terminal.get("TTY") or tty_of(session_id))
    if term in MAC_APPS:
        return MacKeystrokes(MAC_APPS[term])
    return Injector()


# -- reading the terminal -------------------------------------------------------
# Claude Code tells nobody when the mode is switched in the terminal (Shift+Tab fires no hook), nor that a
# typed message is waiting in its queue. Both are on the screen, under the prompt, so the screen is read.
# Only where the terminal can be asked for its text: iTerm2 and Terminal.app.
ITERM_SCREEN_SCRIPT = '''on run argv
  set wanted to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if (unique id of s) is wanted then return contents of s
        end repeat
      end repeat
    end repeat
  end tell
  return ""
end run'''

TERMINAL_SCREEN_SCRIPT = '''on run argv
  set ttyName to item 1 of argv
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if (tty of t) is ttyName then return contents of t
      end repeat
    end repeat
  end tell
  return ""
end run'''


def screen_text(session_id: str, terminal: dict, tty_of=session_tty) -> str | None:
    """What is visible in the session's terminal tab, or None where that cannot be read."""
    try:
        if terminal.get("ITERM_SESSION_ID"):
            return _run(["osascript", "-e", ITERM_SCREEN_SCRIPT, terminal["ITERM_SESSION_ID"].split(":")[-1]]) or None
        if terminal.get("TERM_PROGRAM") == "Apple_Terminal":
            tty = terminal.get("TTY") or tty_of(session_id)
            return (_run(["osascript", "-e", TERMINAL_SCREEN_SCRIPT, tty]) or None) if tty else None
    except InjectError:
        return None
    return None


MODE_WORDS = (("accept edits on", "acceptEdits"), ("plan mode on", "plan"), ("auto mode on", "auto"),
              ("bypass permissions on", "bypassPermissions"), ("don't ask", "dontAsk"))


def read_screen(text: str) -> dict:
    """What Claude Code shows at the bottom of its screen: {"mode": the permission mode or None if it cannot be
    told, "queued": a message is waiting ("... to send now")}.

    The mode line is under the prompt box ("⏵⏵ auto mode on (shift+tab to cycle)"); the normal mode shows
    nothing there, so it is only reported when the prompt box itself is on the screen (not while a dialog is)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()][-16:]
    low = [ln.lower() for ln in lines]
    queued = any("to send now" in ln or "queued \u00b7" in ln for ln in low)   # "Queued · ctrl+enter to send now"
    borders = [i for i, ln in enumerate(lines) if len(ln) >= 20 and set(ln) <= set("─━═")]
    box = None
    for a, b in zip(borders, borders[1:]):
        if b - a <= 12 and any(lines[i][:1] in "❯>" for i in range(a + 1, b)):
            box = (a, b)
    footer = low[box[1] + 1:] if box else low
    for words, mode in MODE_WORDS:
        if any(words in ln for ln in footer):
            return {"mode": mode, "queued": queued}
    return {"mode": "default" if box else None, "queued": queued}
