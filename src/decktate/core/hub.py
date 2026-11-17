"""Session state machine and decision broker.

Claude Code posts hook events here. Most only update a session's state. Two
are held open until a physical key decides them:

  PermissionRequest            -> allow / deny / always-allow (this session)
  PreToolUse(AskUserQuestion)  -> the chosen answers

Safety rule: a held request is released with *no decision* whenever the deck
cannot answer it (nobody attached, timeout, the session moved on, shutdown).
Claude Code then shows its normal prompt in the terminal. Nothing in here
ever approves on its own.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..status import Status
from .audit import Audit


class State(str, Enum):
    IDLE = "idle"  # nothing going on
    WORKING = "working"
    PERMISSION = "permission"  # needs approve / reject
    QUESTION = "question"  # needs an answer to a menu
    WAITING = "waiting"  # Claude is waiting for your next prompt
    DONE = "done"  # finished a turn you have not looked at yet
    ERROR = "error"

    @property
    def needs_you(self) -> bool:
        return self in (State.PERMISSION, State.QUESTION, State.WAITING, State.DONE, State.ERROR)

    @property
    def status(self) -> Status:
        return {State.IDLE: Status.IDLE, State.WORKING: Status.RUNNING, State.PERMISSION: Status.WARN,
                State.QUESTION: Status.WARN, State.WAITING: Status.OK, State.DONE: Status.OK,
                State.ERROR: Status.FAIL}[self]


LIMIT_KEYS = ("five_hour_pct", "five_hour_reset", "seven_day_pct", "seven_day_reset")
SELF_ANSWERING = ("AskUserQuestion", "ExitPlanMode")  # "allow" needs updatedInput for these


@dataclass
class Pending:
    """A hook request that is being held open for a key press."""

    kind: str  # permission | question
    tool_name: str
    tool_input: dict
    suggestions: list = field(default_factory=list)
    created: float = 0.0
    done: threading.Event = field(default_factory=threading.Event)
    response: dict | None = None  # None = no decision: fall through to the terminal

    @property
    def questions(self) -> list[dict]:
        return list(self.tool_input.get("questions") or [])

    @property
    def always_rules(self) -> list[dict]:
        """What "always allow" would add, restricted to this session."""
        out = []
        for s in self.suggestions:
            if isinstance(s, dict) and s.get("type") == "addRules" and s.get("behavior", "allow") == "allow":
                out.append({**s, "destination": "session"})
        return out

    @property
    def always_label(self) -> str:
        rules = [r for s in self.always_rules for r in s.get("rules", [])]
        if not rules:
            return ""
        r = rules[0]
        return r.get("toolName", "") + (f"({r['ruleContent']})" if r.get("ruleContent") else "")


@dataclass
class Session:
    id: str
    cwd: str = ""
    title: str = ""
    state: State = State.IDLE
    mode: str = "default"  # default | acceptEdits | plan | auto | dontAsk | bypassPermissions
    model: str = ""
    activity: str = ""  # what it is doing right now (tool name)
    last_message: str = ""  # last assistant message (from Stop)
    notice: str = ""  # last notification text
    pending: Pending | None = None
    stats: dict = field(default_factory=dict)  # from the status line
    terminal: dict = field(default_factory=dict)  # TERM_PROGRAM, ITERM_SESSION_ID, TMUX_PANE ... of the session
    subagents: int = 0
    started: float = 0.0
    updated: float = 0.0
    pid: int = 0  # the Claude Code process, once known (from its session file)
    turns: int = 0  # how many turns it has ended (Stop / StopFailure)
    transcript: str = ""  # path of its transcript file (.jsonl)
    heard: bool = False  # it has sent a hook: its state is known and its requests can be answered here

    @property
    def name(self) -> str:
        return self.title or os.path.basename(self.cwd.rstrip("/")) or self.id[:6]


def describe(tool_name: str, tool_input: dict) -> str:
    """One short phrase for what a tool call wants to do."""
    ti = tool_input or {}
    if tool_name == "Bash":
        return ti.get("description") or (ti.get("command") or "run a command")[:120]
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        return f"{tool_name.lower()} {os.path.basename(path)}".strip()
    if tool_name in ("WebFetch", "WebSearch"):
        return f"{tool_name} {ti.get('url') or ti.get('query') or ''}".strip()
    if tool_name == "ExitPlanMode":
        return "approve the plan"
    if tool_name.startswith("mcp__"):
        return tool_name.split("__", 2)[-1].replace("_", " ")
    return tool_name


class Hub:
    """All sessions, the selected one, and the pending decisions."""

    def __init__(self, hold_s: float = 120.0, audit: Audit | None = None,
                 clock: Callable[[], float] = time.time, max_sessions: int = 4):
        self.hold_s, self.audit, self.clock, self.max_sessions = hold_s, audit, clock, max_sessions
        self.sessions: dict[str, Session] = {}
        self.selected: str | None = None
        self.limits: dict = {}  # the newest 5-hour / 7-day quota from any session (they are per account, not per session)
        self.detached: set[str] = set()  # sessions the deck was told to leave alone: their hooks pass straight through
        self.attached = False  # a deck (or simulator) is connected and can answer
        self.version = 0  # bumped on every change; the UI redraws when it moves
        self._lock = threading.RLock()
        self._listeners: list[Callable[[str, Session], None]] = []

    # -- plumbing ------------------------------------------------------------
    def subscribe(self, fn: Callable[[str, Session], None]) -> None:
        """fn(event, session) for: permission, question, done, error, waiting, started, ended."""
        self._listeners.append(fn)

    def _emit(self, event: str, session: Session) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, session)
            except Exception:
                pass

    def _touch(self, s: Session) -> None:
        s.updated = self.clock()
        self.version += 1

    def ordered(self) -> list[Session]:
        """All sessions, oldest first."""
        with self._lock:
            return sorted(self.sessions.values(), key=lambda s: s.started)

    def current(self) -> Session | None:
        with self._lock:
            if self.selected in self.sessions:
                return self.sessions[self.selected]
            # nothing selected (first session, or the selected one ended): take one and stay with it
            needy = [s for s in self.ordered() if s.pending]
            pick = needy[0] if needy else (self.ordered()[-1] if self.sessions else None)
            self.selected = pick.id if pick else None
            return pick

    def select(self, session_id: str) -> Session | None:
        with self._lock:
            s = self.sessions.get(session_id)
            if s:
                self.selected = session_id
                if s.state in (State.DONE, State.WAITING, State.ERROR):  # looked at: stop blinking
                    s.state = State.IDLE
                self._touch(s)
            return s

    def _session(self, payload: dict, env: dict | None) -> Session:
        sid = payload.get("session_id") or "unknown"
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = Session(sid, started=self.clock())
            if len(self.sessions) > 12:  # forget the oldest sessions that are not waiting for anything
                for old in sorted(self.sessions.values(), key=lambda x: x.updated)[: len(self.sessions) - 12]:
                    if not old.pending and old.id != sid:
                        del self.sessions[old.id]
        s.cwd = payload.get("cwd") or s.cwd
        s.transcript = payload.get("transcript_path") or s.transcript
        s.title = payload.get("session_title") or s.title
        if payload.get("permission_mode"):
            s.mode = payload["permission_mode"]
        if env:
            s.terminal.update({k: v for k, v in env.items() if v})
        return s

    def _release(self, s: Session, response: dict | None = None) -> None:
        """End a held request. response=None means: no decision, the terminal asks."""
        p, s.pending = s.pending, None
        if p and not p.done.is_set():
            p.response = response
            p.done.set()

    # -- hook events ---------------------------------------------------------
    def handle(self, payload: dict, env: dict | None = None) -> dict:
        """Process one hook event and return the JSON body to answer with.
        Blocks for the two held events; everything else returns {} at once."""
        event = payload.get("hook_event_name", "")
        hold: Pending | None = None
        emit: list[str] = []
        with self._lock:
            if payload.get("session_id") in self.detached:  # not ours: no key, no voice, the terminal asks
                if event == "SessionEnd":
                    self.detached.discard(payload["session_id"])
                    self.version += 1
                return {}
            s = self._session(payload, env)
            s.heard = True
            tool = payload.get("tool_name", "")
            if event == "PermissionRequest":
                if tool in SELF_ANSWERING and tool != "ExitPlanMode":
                    return {}  # a question we did not answer: leave it to the terminal
                self._release(s)
                if self.attached:
                    hold = s.pending = Pending("permission", tool, payload.get("tool_input") or {},
                                               payload.get("permission_suggestions") or [], self.clock())
                    # the selection is NOT moved here: only a key press changes which session you talk to
                s.state, s.activity = State.PERMISSION, tool
                emit.append("permission")
            elif event == "PreToolUse" and tool == "AskUserQuestion":
                self._release(s)
                if self.attached and (payload.get("tool_input") or {}).get("questions"):
                    hold = s.pending = Pending("question", tool, payload.get("tool_input") or {}, [], self.clock())
                s.state, s.activity = State.QUESTION, tool
                emit.append("question")
            else:
                # anything else means the session moved on: a held request is stale
                if s.pending and event in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied",
                                           "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd"):
                    self._release(s)
                if event == "SessionStart":
                    s.model = payload.get("model") or s.model
                    s.state = State.IDLE
                    emit.append("started")
                elif event == "UserPromptSubmit":
                    s.state, s.activity, s.notice = State.WORKING, "thinking", ""
                elif event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
                    s.state, s.activity = State.WORKING, tool
                elif event == "SubagentStart":
                    s.subagents += 1
                elif event == "SubagentStop":
                    s.subagents = max(0, s.subagents - 1)
                elif event == "PreCompact":
                    s.state, s.activity = State.WORKING, "compacting"
                elif event == "Notification":
                    kind = payload.get("notification_type", "")
                    s.notice = payload.get("message") or ""
                    if kind == "permission_prompt" and s.state != State.PERMISSION:
                        s.state = State.PERMISSION  # asked in the terminal only (not held here)
                        emit.append("permission")
                    elif kind in ("idle_prompt", "agent_needs_input") and not s.pending:
                        s.state = State.WAITING
                        emit.append("waiting")
                elif event == "Stop":
                    s.last_message = payload.get("last_assistant_message") or s.last_message
                    s.state, s.activity, s.subagents = State.DONE, "", 0
                    s.turns += 1
                    emit.append("done")
                elif event == "StopFailure":
                    s.last_message = payload.get("last_assistant_message") or payload.get("error") or "error"
                    s.state, s.activity = State.ERROR, ""
                    s.turns += 1
                    emit.append("error")
                elif event == "SessionEnd":
                    self.sessions.pop(s.id, None)
                    if self.selected == s.id:
                        self.selected = None
                    self.version += 1
                    emit.append("ended")
            self._touch(s)
        for e in emit:
            self._emit(e, s)
        if hold is None:
            return {}
        hold.done.wait(self.hold_s)
        with self._lock:
            if s.pending is hold:  # timed out: give the request back to the terminal
                s.pending = None
                self._touch(s)
        return hold.response or {}

    def statusline(self, payload: dict) -> None:
        """Stats from the status line command (model, context, cost, rate limits)."""
        with self._lock:
            sid = payload.get("session_id")
            if not sid or sid in self.detached:
                return
            s = self.sessions.get(sid) or self._session(payload, None)
            ctx, cost, limits = payload.get("context_window") or {}, payload.get("cost") or {}, payload.get("rate_limits") or {}
            s.model = (payload.get("model") or {}).get("display_name") or s.model
            s.title = payload.get("session_name") or s.title
            s.stats = {
                "context_pct": ctx.get("used_percentage"), "context_size": ctx.get("context_window_size"),
                "cost_usd": cost.get("total_cost_usd"), "lines_added": cost.get("total_lines_added"),
                "lines_removed": cost.get("total_lines_removed"), "duration_ms": cost.get("total_duration_ms"),
                "five_hour_pct": (limits.get("five_hour") or {}).get("used_percentage"),
                "five_hour_reset": (limits.get("five_hour") or {}).get("resets_at"),
                "seven_day_pct": (limits.get("seven_day") or {}).get("used_percentage"),
                "seven_day_reset": (limits.get("seven_day") or {}).get("resets_at"),
                "effort": (payload.get("effort") or {}).get("level"), "version": payload.get("version"),
            }
            # the quota is the account's: what one session reports holds for all of them
            self.limits.update({k: v for k, v in s.stats.items() if k in LIMIT_KEYS and v is not None})
            for other in self.sessions.values():
                other.stats.update(self.limits)
            self._touch(s)

    # -- decisions: only ever called from a key press ------------------------
    def decide(self, session_id: str, verdict: str, message: str = "", mode: str = "") -> bool:
        """verdict: allow | always | deny. Returns False if nothing was pending."""
        with self._lock:
            s = self.sessions.get(session_id)
            p = s.pending if s else None
            if not p or p.kind != "permission":
                return False
            if verdict == "deny":
                decision = {"behavior": "deny", "message": message or "Rejected from the Stream Deck."}
            else:
                decision = {"behavior": "allow"}
                if p.tool_name in SELF_ANSWERING:
                    decision["updatedInput"] = p.tool_input
                updates = list(p.always_rules) if verdict == "always" else []
                if mode:
                    updates.append({"type": "setMode", "mode": mode, "destination": "session"})
                if updates:
                    decision["updatedPermissions"] = updates
            if self.audit:
                self.audit.write("permission", s.id, verdict=verdict, tool=p.tool_name, cwd=s.cwd,
                                 request=describe(p.tool_name, p.tool_input)[:200],
                                 rule=p.always_label if verdict == "always" else "", message=message)
            self._release(s, {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}})
            s.state = State.WORKING if verdict != "deny" else State.IDLE
            self._touch(s)
            return True

    def answer(self, session_id: str, answers: dict[str, str]) -> bool:
        """Answer a held AskUserQuestion. answers: question text -> label(s), comma-joined for multi-select."""
        with self._lock:
            s = self.sessions.get(session_id)
            p = s.pending if s else None
            if not p or p.kind != "question":
                return False
            if self.audit:
                self.audit.write("answer", s.id, cwd=s.cwd, answers=answers)
            self._release(s, {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "allow",
                "permissionDecisionReason": "Answered on the Stream Deck",
                "updatedInput": {**p.tool_input, "answers": answers}}})
            s.state = State.WORKING
            self._touch(s)
            return True

    def hand_back(self, session_id: str, why: str = "cancelled") -> bool:
        """Give a held request back to the terminal without deciding."""
        with self._lock:
            s = self.sessions.get(session_id)
            if not s or not s.pending:
                return False
            if self.audit:
                self.audit.write("handed_back", s.id, kind=s.pending.kind, why=why)
            self._release(s)
            self._touch(s)
            return True

    # -- which sessions the deck looks after ------------------------------------
    def attach(self, session_id: str, cwd: str = "", title: str = "", pid: int = 0, terminal: dict | None = None,
               started: float | None = None) -> Session:
        """Look after a session: one that was detached, or a running one that has not sent a hook yet."""
        with self._lock:
            self.detached.discard(session_id)
            s = self.sessions.get(session_id)
            if s is None:
                s = self.sessions[session_id] = Session(session_id, cwd=cwd, title=title,
                                                        started=started or self.clock())
            s.pid = pid or s.pid
            s.stats.update(self.limits)
            if terminal:
                s.terminal.update({k: v for k, v in terminal.items() if v and not s.terminal.get(k)})
            self._touch(s)
            return s

    def detach(self, session_id: str) -> bool:
        """Leave a session alone: a request held for it goes back to its terminal, its key disappears,
        and from now on its hooks are answered at once without any effect here."""
        with self._lock:
            s = self.sessions.pop(session_id, None)
            self.detached.add(session_id)
            if s and s.pending:
                if self.audit:
                    self.audit.write("handed_back", s.id, kind=s.pending.kind, why="detached")
                self._release(s)
            if self.selected == session_id:
                self.selected = None
            self.version += 1
            return s is not None

    def forget(self, session_id: str) -> None:
        """The session's process is gone."""
        with self._lock:
            s = self.sessions.pop(session_id, None)
            self.detached.discard(session_id)
            if s:
                self._release(s)
            if self.selected == session_id:
                self.selected = None
            self.version += 1

    def set_attached(self, attached: bool) -> None:
        """Deck connected or gone. Losing the deck releases everything that is held."""
        with self._lock:
            self.attached = attached
            if not attached:
                for s in self.sessions.values():
                    self._release(s)
            self.version += 1

    def shutdown(self) -> None:
        self.set_attached(False)
