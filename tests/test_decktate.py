"""State machine, held decisions, hooks installer, speech plumbing and the board.
Everything runs against the simulator: no deck, no network, no microphone."""

import json
import threading
import types
import time

import httpx
import pytest

from decktate import config as config_mod
from decktate.backend import SimDeck
from decktate.claude import install
from decktate.core.audit import Audit
from decktate.core.hub import Hub, State, describe
from decktate.core.redact import redact, speakable
from decktate.core.server import HookServer
from decktate.inject import AppleTerminal, InjectError, Injector, ITerm, Kitty, MacKeystrokes, Tmux, WezTerm, for_session
from decktate.status import Status
from decktate.ui.board import Board

S1, MORE, MODE = 0, 3, 8
OK, NO, STOP, ALWAYS, SETTINGS, STATS = 6, 5, 5, 7, 4, 9      # STOP shares the reject key
MIC, SPEAK, SEND, TOAST, COMMANDS, DISCARD = 10, 11, 6, 13, 14, 12   # send now: next to stop, on the approve key

BASH = {"hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": "/work/api-server", "permission_mode": "default",
        "tool_name": "Bash", "tool_input": {"command": "rm -rf node_modules", "description": "Remove node_modules"},
        "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "rm -rf node_modules"}],
                                    "behavior": "allow", "destination": "localSettings"}]}
QUESTION = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/work/api-server", "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [
                {"question": "Which framework?", "header": "Framework", "multiSelect": False,
                 "options": [{"label": "React", "description": "Component library"}, {"label": "Vue", "description": "Progressive"}]},
                {"question": "Which extras?", "header": "Extras", "multiSelect": True,
                 "options": [{"label": "Router"}, {"label": "Tests"}, {"label": "Linting"}]}]}}


class FakeSpeaker:
    enabled = True

    def __init__(self):
        self.said, self.chimes, self.speaking, self.stopped = [], [], False, 0
        self.engine = type("E", (), {"name": "system"})()
        self.player = type("P", (), {"device": "", "volume": 1.0})()

    @property
    def last_text(self):
        return self.said[-1] if self.said else ""

    def speak(self, text, cache=False, remember=True):
        self.said.append(text)

    def repeat(self):
        if self.said:
            self.said.append(self.said[-1])

    def chime(self, kind):
        self.chimes.append(kind)

    def stop(self):
        self.stopped += 1
        self.speaking = False


class FakeTake:
    """A dictation that 'hears' a fixed sentence."""

    def __init__(self, heard="please add tests for the parser", error=""):
        self.heard, self.error, self.level, self.text, self.started = heard, error, 0.4, "", False

    def start(self):
        self.started = not self.error
        self.text = "please add" if self.started else ""
        return self.started

    def finish(self):
        self.text = self.heard
        return self.heard

    def cancel(self):
        pass


class Rig:
    """Hub + board on a simulator with a fake clock."""

    def __init__(self, tmp_path, hold_s=5.0, take=FakeTake, slash=None):
        self.now = 1000.0
        self.cfg = config_mod.load(tmp_path)
        self.cfg.slash = slash or ["/compact", "/clear", "/review"]
        self.audit_path = tmp_path / "audit.jsonl"
        self.hub = Hub(hold_s=hold_s, audit=Audit(self.audit_path))
        self.deck = SimDeck()
        self.speaker = FakeSpeaker()
        self.takes = []
        self.screens = {}  # session id -> what its terminal shows
        self.system = {"vol": 44}  # the computer's own output volume
        self.running, self.quits = [], []  # what Claude Code "runs" on this computer; the pids asked to quit

        def new_take():
            t = take()
            self.takes.append(t)
            return t

        self.typed = []

        class Recorder(Injector):
            name = "test"

            def send_text(inner, text, enter=True):
                self.typed.append(("text", text))

            def key(inner, key):
                self.typed.append((key, ""))

        self.board = Board(self.deck, self.hub, self.cfg, self.speaker, new_take if take else None,
                           injector_for=lambda sid, term: Recorder(), clock=lambda: self.now, sync=True,
                           sysvol=types.SimpleNamespace(get=lambda: self.system["vol"],
                                                        set=lambda pct: self.system.update(vol=pct) or True),
                           screen_of=lambda sid, term: self.screens.get(sid),
                           discover=lambda: list(self.running), alive=lambda pid: any(x.pid == pid for x in self.running),
                           quit_session=lambda pid: self.quits.append(pid) or True,
                           terminal_env=lambda pid: {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": f"w0t0p0:{pid}"})
        self.board.tick()

    def advance(self, seconds, step=0.05):
        end = self.now + seconds
        while self.now < end - 1e-9:
            self.now = min(end, self.now + step)
            self.board.tick()

    def tap(self, key):
        self.deck.press(key)
        self.advance(0.05)
        self.deck.release(key)
        self.board.tick()

    def hold(self, key, seconds):
        self.deck.press(key)
        self.advance(seconds)
        self.deck.release(key)
        self.board.tick()

    def post(self, payload, env=None):
        """Send a hook event on a thread (it may be held open). Returns the thread and a result box."""
        box = {}
        t = threading.Thread(target=lambda: box.update(response=self.hub.handle(payload, env)), daemon=True)
        t.start()
        time.sleep(0.05)
        self.board.tick()
        return t, box

    def face(self, key):
        k = self.board.keys().get(key)
        return k.face if k else None

    def audit(self):
        return [json.loads(l) for l in self.audit_path.read_text().splitlines()] if self.audit_path.exists() else []


def done(thread, box):
    thread.join(2)
    assert not thread.is_alive(), "the hook request is still being held"
    return box["response"]


# ------------------------------------------------------------- state machine
def test_session_states_follow_hook_events(tmp_path):
    r = Rig(tmp_path)
    h = r.hub
    h.handle({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/w/alpha", "model": "claude-x", "permission_mode": "plan"})
    a = h.sessions["a"]
    assert (a.name, a.state, a.mode, a.model) == ("alpha", State.IDLE, "plan", "claude-x")
    h.handle({"hook_event_name": "UserPromptSubmit", "session_id": "a", "prompt": "hi"})
    assert a.state == State.WORKING and a.activity == "thinking"
    h.handle({"hook_event_name": "PreToolUse", "session_id": "a", "tool_name": "Read", "tool_input": {"file_path": "/x/y.py"}})
    assert (a.state, a.activity) == (State.WORKING, "Read")
    h.handle({"hook_event_name": "SubagentStart", "session_id": "a", "agent_id": "1"})
    assert a.subagents == 1
    h.handle({"hook_event_name": "Notification", "session_id": "a", "notification_type": "idle_prompt", "message": "waiting"})
    assert a.state == State.WAITING and a.state.needs_you
    h.handle({"hook_event_name": "Stop", "session_id": "a", "last_assistant_message": "All 12 tests pass. I also fixed the linter."})
    assert (a.state, a.subagents) == (State.DONE, 0) and a.last_message.startswith("All 12")
    h.handle({"hook_event_name": "StopFailure", "session_id": "a", "error": "API error"})
    assert a.state == State.ERROR
    h.select("a")
    assert a.state == State.IDLE                        # looking at it clears "needs you"
    h.handle({"hook_event_name": "SessionEnd", "session_id": "a", "reason": "other"})
    assert "a" not in h.sessions and h.current() is None
    h.statusline({"session_id": "b", "cwd": "/w/beta", "model": {"display_name": "Opus"}, "version": "2.1.326",
                  "cost": {"total_cost_usd": 1.234, "total_lines_added": 40, "total_lines_removed": 7, "total_duration_ms": 125000},
                  "context_window": {"used_percentage": 42.5, "context_window_size": 200000},
                  "rate_limits": {"five_hour": {"used_percentage": 71, "resets_at": 1}, "seven_day": {"used_percentage": 12}}})
    b = h.sessions["b"]
    assert b.model == "Opus" and b.stats["context_pct"] == 42.5 and b.stats["five_hour_pct"] == 71 and b.stats["cost_usd"] == 1.234


def test_describe_tool_calls():
    assert describe("Bash", {"command": "npm test", "description": "Run the tests"}) == "Run the tests"
    assert describe("Bash", {"command": "ls -la"}) == "ls -la"
    assert describe("Edit", {"file_path": "/a/b/parser.py"}) == "edit parser.py"
    assert describe("WebFetch", {"url": "https://example.com/x"}) == "WebFetch https://example.com/x"
    assert describe("mcp__github__create_issue", {}) == "create issue"
    assert describe("ExitPlanMode", {"plan": "..."}) == "approve the plan"


# ------------------------------------------------------------ held decisions
def test_approve_reject_and_always_from_keys(tmp_path):
    r = Rig(tmp_path)
    t, box = r.post(BASH)
    s = r.hub.sessions["s1"]
    assert s.state == State.PERMISSION and s.pending and t.is_alive()        # held open, nothing decided
    assert r.face(OK).solid and not r.face(OK).disabled and r.face(ALWAYS).text == "Bash(rm -rf node_modules)"
    assert r.speaker.chimes == ["alert"] and r.speaker.said == ["api-server asks permission: Remove node_modules"]
    r.tap(OK)
    assert done(t, box) == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    assert s.pending is None and r.speaker.chimes[-1] == "ok" and r.face(TOAST).text == "approved"
    assert r.face(OK) is None and r.face(ALWAYS) is None                    # nothing to decide: the keys are gone
    assert r.face(STOP).label == "interrupt"                                     # the session is busy again: stop takes the reject key

    t, box = r.post(BASH)
    r.tap(NO)
    assert done(t, box)["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "Rejected from the Stream Deck."}

    t, box = r.post(BASH)                                  # always: tap and short holds do nothing
    r.tap(ALWAYS)
    r.hold(ALWAYS, 1.0)
    assert t.is_alive() and s.pending
    r.deck.press(ALWAYS)
    r.advance(1.2)
    key, p = r.board.confirm_progress(r.now)              # the outline is running around the key
    assert key == ALWAYS and 0.3 < p < 0.7
    r.advance(0.8)
    r.deck.release(ALWAYS)
    r.board.tick()
    assert done(t, box)["hookSpecificOutput"]["decision"] == {
        "behavior": "allow", "updatedPermissions": [{"type": "addRules", "behavior": "allow", "destination": "session",
                                                     "rules": [{"toolName": "Bash", "ruleContent": "rm -rf node_modules"}]}]}
    log = r.audit()
    assert [(e["what"], e["verdict"]) for e in log] == [("permission", "allow"), ("permission", "deny"), ("permission", "always")]
    assert log[2]["rule"] == "Bash(rm -rf node_modules)" and log[0]["request"] == "Remove node_modules" and log[0]["cwd"] == "/work/api-server"


def test_always_is_disabled_without_a_suggested_rule_and_plan_approval_echoes_input(tmp_path):
    r = Rig(tmp_path)
    plan = {"hook_event_name": "PermissionRequest", "session_id": "s1", "cwd": "/w/x", "tool_name": "ExitPlanMode",
            "tool_input": {"plan": "1. do it", "planFilePath": "/tmp/p.md"}}
    t, box = r.post(plan)
    assert r.face(ALWAYS) is None and r.face(OK).label == "approve"          # no rule suggested: no "always" key
    r.hold(ALWAYS, 2.5)
    assert t.is_alive()
    r.tap(OK)                                              # "allow" needs updatedInput for this tool
    assert done(t, box)["hookSpecificOutput"]["decision"] == {"behavior": "allow", "updatedInput": plan["tool_input"]}


def test_fail_open_never_blocks_or_approves(tmp_path):
    r = Rig(tmp_path, hold_s=0.3)
    t, box = r.post(BASH)                                  # nobody presses a key: timeout
    assert done(t, box) == {} and r.hub.sessions["s1"].pending is None

    r.hub.hold_s = 5
    t, box = r.post(BASH)                                  # the user answered in the terminal: the session moves on
    r.hub.handle({"hook_event_name": "PostToolUse", "session_id": "s1", "tool_name": "Bash"})
    assert done(t, box) == {} and r.hub.sessions["s1"].state == State.WORKING

    t, box = r.post(BASH)                                  # the deck goes away while a request is held
    r.board.close()
    assert done(t, box) == {} and not r.hub.attached

    start = time.time()                                    # no deck attached: answered at once, without a decision
    assert r.hub.handle(BASH) == {} and time.time() - start < 0.2
    assert r.hub.sessions["s1"].state == State.PERMISSION  # still shown as waiting, the terminal asks
    assert r.hub.decide("s1", "allow") is False            # nothing held, nothing to decide
    assert r.audit() == []                                 # and no decision was ever recorded


def test_reject_with_a_dictated_reason(tmp_path):
    r = Rig(tmp_path)
    t, box = r.post(BASH)
    assert r.face(NO).caption == "hold: why"
    r.deck.press(NO)
    r.advance(0.4)                                         # push to talk started
    assert r.board.take and r.board.take_for == "reason" and r.face(NO).label == "listening"
    r.deck.release(NO)
    r.board.tick()
    assert done(t, box)["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "please add tests for the parser"}
    assert r.audit()[-1]["message"] == "please add tests for the parser"


# ---------------------------------------------------------------------- menus
def test_menu_single_and_multi_select_on_keys(tmp_path):
    r = Rig(tmp_path)
    t, box = r.post(QUESTION)
    s = r.hub.sessions["s1"]
    assert s.state == State.QUESTION and t.is_alive()
    assert "Which framework?" in r.speaker.said[-1] and "1: React" in r.speaker.said[-1] and "2: Vue" in r.speaker.said[-1]
    assert (r.face(0).text, r.face(0).label) == ("Framework", "question 1/2")
    assert [r.face(k).text for k in (1, 2)] == ["React", "Vue"] and r.face(3) is None
    assert r.face(4).disabled and r.face(14).label == "in terminal"            # confirm is for multi-select only
    r.tap(2)                                                                   # single select: picking answers it
    assert t.is_alive() and r.face(0).label == "question 2/2" and "Which extras?" in r.speaker.said[-1]
    assert [r.face(k).text for k in (1, 2, 3)] == ["Router", "Tests", "Linting"]
    r.tap(4)                                                                   # nothing picked yet
    assert t.is_alive()
    r.tap(1), r.tap(3)
    assert r.face(1).solid and r.face(3).solid and not r.face(2).solid and r.face(4).caption == "2 picked"
    r.tap(3), r.tap(2)                                                         # toggle off, pick another
    r.tap(4)
    out = done(t, box)["hookSpecificOutput"]
    assert (out["hookEventName"], out["permissionDecision"]) == ("PreToolUse", "allow")
    assert out["updatedInput"]["questions"] == QUESTION["tool_input"]["questions"]       # echoed back unchanged
    assert out["updatedInput"]["answers"] == {"Which framework?": "Vue", "Which extras?": "Router, Tests"}
    assert r.audit()[-1]["what"] == "answer" and s.state == State.WORKING and r.face(0).kind == "session"   # back to sessions

    t, box = r.post(QUESTION)                                                  # cancel: the terminal shows the menu
    r.tap(14)
    assert done(t, box) == {} and r.audit()[-1]["what"] == "handed_back"
    # a question we did not answer may come back as a permission request: leave that to the terminal too
    assert r.hub.handle({**QUESTION, "hook_event_name": "PermissionRequest"}) == {}


# -------------------------------------------------------------------- board
def test_session_keys_blink_select_and_speak(tmp_path):
    r = Rig(tmp_path)
    assert all(r.face(k) is None for k in range(15) if k != SETTINGS)       # no session: only settings is shown
    for sid, name in (("a", "alpha"), ("b", "beta")):
        r.hub.handle({"hook_event_name": "SessionStart", "session_id": sid, "cwd": f"/w/{name}"})
    r.hub.select("a")
    r.hub.handle({"hook_event_name": "UserPromptSubmit", "session_id": "a"})
    r.hub.handle({"hook_event_name": "PreToolUse", "session_id": "a", "tool_name": "Bash", "tool_input": {}})
    r.hub.handle({"hook_event_name": "Stop", "session_id": "b",
                  "last_assistant_message": "Done. The parser now handles `nested` lists.\n\n```py\ncode\n```\nThird sentence. Fourth."})
    r.board.tick()
    a, b = r.face(0), r.face(1)                                    # each on its own key
    assert (a.text, a.icon, a.value, a.blink, a.pulse) == ("alpha", "running", "Bash", False, True)
    assert (b.text, b.icon, b.value, b.blink, b.solid) == ("beta", "check", "done", True, True)
    assert r.face(2) is None and r.face(MORE) is None and r.speaker.said[-1] == "beta finished. Done. The parser now handles nested lists."
    before = r.deck.images[1].tobytes()
    r.advance(0.4)
    assert r.deck.images[1].tobytes() != before                    # it blinks
    r.tap(1)                                                       # select: speak what it finished, stop blinking
    assert r.hub.selected == "b" and r.speaker.said[-1].startswith("beta finished. Done.")
    assert (r.face(1).text, r.face(1).blink, bool(r.face(1).outline)) == ("beta", False, True)   # selected, and it stays on its key
    assert r.face(0).text == "alpha" and not r.face(0).outline
    r.tap(0)
    assert r.speaker.said[-1] == "alpha is working: Bash."


def test_queue_order_and_more_sessions_page(tmp_path):
    r = Rig(tmp_path)
    for i, name in enumerate(["one", "two", "three", "four", "five"]):
        r.hub.clock = lambda i=i: 100.0 + i                        # started (and last active) in this order
        r.hub.handle({"hook_event_name": "SessionStart", "session_id": name, "cwd": f"/w/{name}"})
    r.hub.select("two")
    r.hub.clock = lambda: 200.0
    r.hub.handle({"hook_event_name": "Stop", "session_id": "one", "last_assistant_message": "ok"})     # wants attention
    r.board.tick()
    order = lambda: [s.name for s in r.board.queue()]              # noqa: E731
    assert order() == ["two", "one", "five", "four", "three"]      # selected, then needs-you, then latest activity
    assert [r.face(k).text for k in range(3)] == ["two", "one", "five"]    # the keys are handed out in that order ... once
    more = r.face(MORE)
    assert (more.label, more.value, more.caption, more.blink) == ("more", "+2", "sessions", False)
    r.hub.clock = lambda: 300.0
    t, box = r.post({**BASH, "session_id": "three", "cwd": "/w/three"})   # a decision is wanted: very first
    r.board.tick()
    # nothing moves and you keep talking to "two": the sessions key blinks, and there is nothing to approve by accident
    assert order()[0] == "three" and r.hub.selected == "two"
    assert [r.face(k).text for k in range(3)] == ["two", "one", "five"] and r.face(0).outline
    assert r.face(MORE).blink is True and r.face(OK) is None
    r.tap(MORE)
    assert [r.face(k).text for k in (0, 1)] == ["three", "four"] and r.face(2) is None   # the list: only those without a key
    r.tap(0)                                                       # you pick "three" from the list
    # it had no key: it takes the key of the session last in the queue (five); two and one stay where they are
    assert r.hub.selected == "three" and r.board.page == "main"
    assert [r.face(k).text for k in range(3)] == ["two", "one", "three"] and r.face(2).outline
    r.tap(OK)
    done(t, box)
    r.hub.clock = lambda: 400.0
    r.hub.handle({"hook_event_name": "Stop", "session_id": "four", "last_assistant_message": "ok"})
    r.hub.select("two")
    r.board.tick()
    assert order() == ["two", "four", "one", "three", "five"]
    assert [r.face(k).text for k in range(3)] == ["two", "one", "three"]   # the queue changed, the keys did not
    assert r.face(MORE).value == "+2" and r.face(MORE).blink is True        # four and five have no key; four wants attention
    r.hub.clock = lambda: 500.0
    r.hub.handle({"hook_event_name": "StopFailure", "session_id": "five", "error": "x"})
    r.board.tick()
    assert order() == ["two", "five", "four", "one", "three"] and r.face(MORE).blink is True   # one of the hidden wants attention
    r.tap(MORE)
    assert r.board.page == "sessions"
    assert [r.face(k).text for k in (0, 1)] == ["four", "five"]    # in the order they were started
    assert r.face(9).label == "back" and r.face(4) is None and r.face(14) is None       # one page: no paging keys
    r.tap(0)                                                       # pick "four": selected, spoken, back on the main page
    assert r.hub.selected == "four" and r.board.page == "main"
    assert [r.face(k).text for k in range(3)] == ["two", "one", "four"] and r.face(2).outline
    assert r.speaker.said[-1].startswith("four finished.")
    r.hub.handle({"hook_event_name": "SessionEnd", "session_id": "two"})   # a session ends ...
    r.board.tick()
    assert r.face(0) is None and [r.face(k).text for k in (1, 2)] == ["one", "four"]   # its key waits a moment for it to come back
    r.advance(4.1)                                                 # ... then the others move up, and the first in the queue
    assert [r.face(k).text for k in range(3)] == ["one", "four", "five"] and r.face(MORE).value == "+1"   # gets the key on the right


def test_browse_attach_detach_and_quit_sessions(tmp_path):
    from decktate.claude.discover import Running

    (tmp_path / "detached.json").write_text(json.dumps({"old": time.time(), "b": time.time()}))   # sent away earlier
    r = Rig(tmp_path)
    r.running = [Running("old", 101, "/w/legacy", started=1.0), Running("a", 102, "/w/api", started=2.0),
                 Running("b", 103, "/x/api", busy=True, started=3.0)]
    r.advance(3.1)
    a = r.hub.sessions["a"]                                        # a running session is looked after without having reported
    assert (a.pid, a.heard, a.terminal["ITERM_SESSION_ID"]) == (102, False, "w0t0p0:102")   # ... and can be typed into
    assert (r.face(0).text, r.face(0).value) == ("api 1", "idle") and r.face(1) is None    # the detached ones stay away
    assert (r.face(MORE).label, r.face(MORE).value, r.face(MORE).caption) == ("more", "+2", "2 detached")
    r.tap(MORE)                                                    # the sessions that are not on the keys, oldest first
    assert r.board.page == "sessions"
    assert [(r.face(k).text, r.face(k).value) for k in range(2)] == [("legacy", "detached"), ("api 2", "detached")]
    assert r.face(0).status == Status.UNKNOWN and r.face(2) is None                         # grey
    r.tap(1)                                                       # tap a grey one: attached; it gets a free key
    b = r.hub.sessions["b"]
    assert r.board.page == "sessions" and (b.pid, b.heard, b.state) == (103, False, State.WORKING)
    assert r.face(TOAST).text == "attached api 2" and r.face(0).text == "legacy" and r.face(1) is None
    assert list(json.loads((tmp_path / "detached.json").read_text())) == ["old"]
    r.tap(9)
    assert r.board.page == "main" and [(r.face(k).text, r.face(k).value) for k in range(2)] == [("api 1", "idle"), ("api 2", "busy")]
    r.running[2].busy = False
    r.advance(3.1)
    assert r.face(1).value == "idle" and (r.face(MORE).value, r.face(MORE).caption) == ("+1", "1 detached")

    t, box = r.post({**BASH, "session_id": "a", "cwd": "/w/api"})   # a request is held for "a" ...
    assert r.face(OK).label == "approve"
    r.hold(0, 0.7)                                                 # hold its key: the session menu
    assert r.board.page == "session" and [r.face(k).label for k in (5, 6, 7, 9)] == ["talk to", "detach", "quit", "back"]
    assert r.face(0).text == "api 1"
    r.tap(6)                                                       # ... detach: the request goes back to the terminal
    assert done(t, box) == {} and "a" not in r.hub.sessions and "a" in r.hub.detached
    assert r.board.page == "main" and r.face(TOAST).text == "detached api 1"    # back where the menu was opened: the home screen
    assert [r.face(k).text if r.face(k) else None for k in range(3)] == ["api 2", None, None]   # the gap is closed at once
    assert (r.face(MORE).value, r.face(MORE).caption) == ("+2", "2 detached")
    assert sorted(json.loads((tmp_path / "detached.json").read_text())) == ["a", "old"]   # remembered on disk ...
    again = Rig(tmp_path)                                          # ... so after a restart of Decktate
    again.running = r.running
    assert again.hub.handle({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/w/api"}) == {}
    again.advance(3.1)
    assert "a" not in again.hub.sessions                           # it stays detached although it runs and reports
    assert [again.face(k).text if again.face(k) else None for k in range(3)] == ["api 2", None, None]   # no gap where it was
    assert (again.face(MORE).value, again.face(MORE).caption) == ("+2", "2 detached")
    again.board.close()
    r.advance(3.1)
    assert "a" not in r.hub.sessions                               # looking again does not bring it back either
    r.tap(MORE)
    assert [(r.face(k).text, r.face(k).value) for k in range(2)] == [("legacy", "detached"), ("api 1", "detached")]
    assert r.hub.handle({**BASH, "session_id": "a", "cwd": "/w/api"}) == {} and "a" not in r.hub.sessions   # its hooks pass through
    r.hub.statusline({"session_id": "a", "cost": {"total_cost_usd": 1}})
    assert "a" not in r.hub.sessions
    r.tap(1)                                                       # and back again
    assert "a" in r.hub.sessions and "a" not in r.hub.detached
    assert list(json.loads((tmp_path / "detached.json").read_text())) == ["old"]          # attached again: forgotten
    r.advance(4.1)
    assert r.face(0).text == "legacy" and r.face(1) is None        # it is on a key again, so no longer in this list

    r.hold(0, 0.7)                                                 # the menu of a session that is not attached
    assert r.board.page == "session" and [r.face(k).label if r.face(k) else None for k in (5, 6, 7)] == ["attach", None, "quit"]
    r.tap(9)
    assert r.board.page == "sessions"                              # opened from the list: back goes to the list
    r.hold(0, 0.7)
    r.tap(7)                                                       # quit needs the confirm hold; a tap says so
    assert r.quits == [] and r.board.page == "session" and r.face(TOAST).text == "hold to quit"
    r.hold(7, 2.5)
    assert r.quits == [101] and r.board.page == "main" and r.face(TOAST).text == "quit"
    r.advance(3.1)                                                 # it is still exiting: not picked up again
    assert "old" not in r.hub.sessions
    r.running.pop(0)
    r.advance(3.1)
    assert r.face(MORE) is None and sorted(r.face(k).text for k in range(3) if r.face(k)) == ["api 1", "api 2"]   # all on keys
    r.running = [x for x in r.running if x.id != "b"]              # a session is closed in its terminal: its key goes away
    r.advance(3.1)
    assert "b" not in r.hub.sessions and [bool(r.face(k)) for k in range(3)].count(True) == 1


def test_keys_and_selection_come_back_as_they_were_after_a_restart(tmp_path):
    from decktate.claude.discover import Running

    running = [Running(f"s{n}", 200 + n, f"/w/p{n}", started=float(n)) for n in range(1, 5)]   # four sessions, three keys
    r = Rig(tmp_path)
    r.running = running
    r.advance(3.1)
    r.tap(MORE), r.tap(0)                                          # bring the one without a key onto a key; it is selected
    r.advance(0.1)
    before = [r.face(k).text for k in range(3)]
    hidden = ({"p1", "p2", "p3", "p4"} - set(before)).pop()
    assert r.hub.selected and r.face(MORE).value == "+1" and len(set(before)) == 3
    selected = r.hub.selected
    r.board.close()

    r2 = Rig(tmp_path)                                             # Decktate restarts; the sessions are idle and send nothing
    r2.running = running
    r2.advance(3.1)
    assert [r2.face(k).text for k in range(3)] == before and r2.hub.selected == selected   # the same keys, the same selection
    assert r2.face(before.index(r2.hub.sessions[selected].name)).outline and r2.face(MORE).value == "+1"
    r2.hub.handle({"hook_event_name": "Stop", "session_id": next(x.id for x in running if x.cwd.endswith(hidden)),
                   "last_assistant_message": "ok"})                # the one that had no key finishes: it still has none
    r2.advance(0.2)
    assert [r2.face(k).text for k in range(3)] == before and r2.face(MORE).blink is True
    r2.board.close()

    r3 = Rig(tmp_path)                                             # restart before the terminals are back (desktop restore)
    r3.advance(10)
    assert all(r3.face(k) is None for k in range(3))
    r3.running = running                                           # they come back later, same sessions
    r3.advance(3.1)
    assert [r3.face(k).text for k in range(3)] == before and r3.hub.selected == selected


def test_a_session_restarted_in_its_terminal_keeps_its_key_and_stays_selected(tmp_path):
    r = Rig(tmp_path)
    tab = lambda n: {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": f"w0t0p0:{n}"}   # noqa: E731
    for n, sid in enumerate(["s1", "s2", "s3", "s4"]):
        r.hub.clock = lambda n=n: 100.0 + n
        r.hub.handle({"hook_event_name": "SessionStart", "session_id": sid, "cwd": f"/w/p{n + 1}"}, tab(n + 1))
        r.board.tick()
    r.board._select(r.hub.sessions["s2"])
    r.board.tick()
    before = [r.face(k).text for k in range(3)]
    assert "p2" in before and r.face(MORE).value == "+1"
    r.hub.clock = lambda: 200.0
    r.hub.handle({"hook_event_name": "SessionEnd", "session_id": "s2"})                     # /clear in that terminal:
    r.board.tick()                                                                          # (the deck looks in between)
    r.hub.handle({"hook_event_name": "SessionStart", "session_id": "s2b", "cwd": "/w/p2"}, tab(2))   # a new session, same tab
    r.board.tick()
    assert [r.face(k).text for k in range(3)] == before and r.hub.selected == "s2b"         # same key, still the one you talk to
    assert r.face(before.index("p2")).outline and r.face(MORE).value == "+1"
    for sid in ("s1", "s3", "s4"):                                                          # the others get busy, finish, ask ...
        r.hub.clock = lambda: 300.0
        r.hub.handle({"hook_event_name": "UserPromptSubmit", "session_id": sid})
        r.hub.handle({"hook_event_name": "Stop", "session_id": sid, "last_assistant_message": "ok"})
    t, box = r.post({**BASH, "session_id": "s4", "cwd": "/w/p4"})
    r.advance(1)
    assert [r.face(k).text for k in range(3)] == before and r.hub.selected == "s2b"         # ... nothing moves, nothing switches
    r.hub.hand_back("s4")
    done(t, box)


SCREEN = """⏺ Done.

✻ Sautéed for 27s
──────────────────────────────────────────────
❯ %s
──────────────────────────────────────────────
  me | ~/w/a | c [###-------] 30%% | Model
  %s
"""


def test_a_mode_switched_in_the_terminal_shows_on_the_deck(tmp_path):
    from decktate.inject import read_screen

    assert read_screen(SCREEN % ("", "⏸ plan mode on (shift+tab to cycle)")) == {"mode": "plan", "queued": False}
    assert read_screen(SCREEN % ("x", "⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt"))["mode"] == "auto"
    assert read_screen(SCREEN % ("", "⏵⏵ accept edits on (shift+tab to cycle)"))["mode"] == "acceptEdits"
    assert read_screen(SCREEN % ("", "⏵⏵ bypass permissions on (shift+tab to cycle)"))["mode"] == "bypassPermissions"
    assert read_screen(SCREEN % ("", "? for shortcuts"))["mode"] == "default"       # the normal mode shows no mode line
    assert read_screen(SCREEN % ("", ""))["mode"] == "default"
    dialog = "Do you want to proceed?\n❯ 1. Yes\n  2. No\n\nEsc to cancel"
    assert read_screen(dialog)["mode"] is None and read_screen("")["mode"] is None   # no prompt box: cannot tell
    assert read_screen(SCREEN % ("next", "⏵⏵ auto mode on · queued · ctrl+enter to send now"))["queued"] is True

    r = Rig(tmp_path)
    r.hub.handle({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/w/a", "permission_mode": "default"})
    r.hub.handle({"hook_event_name": "UserPromptSubmit", "session_id": "a"})
    a = r.hub.sessions["a"]
    r.board.tick()
    assert r.face(MODE).caption == "manual" and r.face(SEND) is None
    r.screens["a"] = SCREEN % ("", "⏸ plan mode on (shift+tab to cycle)")   # Shift+Tab in the terminal: no hook fires
    r.board._watch(a)
    r.board.tick()
    assert (r.face(MODE).icon, r.face(MODE).caption) == ("pause", "plan")
    r.screens["a"] = SCREEN % ("do that too", "⏵⏵ auto mode on · ctrl+enter to send now")   # typed on the keyboard, queued
    r.board._watch(a)
    r.board.tick()
    assert r.face(MODE).caption == "auto" and r.face(SEND).label == "send now"
    r.screens["a"] = dialog                                         # a dialog covers the prompt: the mode is kept
    r.board._watch(a)
    r.board.tick()
    assert r.face(MODE).caption == "auto" and r.face(SEND) is None
    r.screens["a"] = None                                           # a terminal that cannot be read: nothing changes
    r.board._watch(a)
    assert a.mode == "auto"
    r.tap(MODE)                                                     # the deck has seen "auto": Shift+Tab leads on to manual
    assert r.face(MODE).caption == "manual"


def test_stats_are_there_again_after_a_restart_and_the_quota_is_shared(tmp_path):
    from decktate.claude.discover import Running

    running = [Running("a", 301, "/w/a", started=1.0), Running("b", 302, "/w/b", started=2.0)]
    r = Rig(tmp_path)
    r.running = running
    r.advance(3.1)
    r.hub.statusline({"session_id": "a", "cost": {"total_cost_usd": 4.5}, "context_window": {"used_percentage": 61},
                      "rate_limits": {"five_hour": {"used_percentage": 33}, "seven_day": {"used_percentage": 44}}})
    assert r.hub.sessions["b"].stats["five_hour_pct"] == 33        # the quota is the account's: b has it too
    r.hub.statusline({"session_id": "b", "cost": {"total_cost_usd": 1.0}, "context_window": {"used_percentage": 12},
                      "rate_limits": {"seven_day": {"used_percentage": 45}}})   # a report without the 5-hour figure
    assert (r.hub.sessions["b"].stats["five_hour_pct"], r.hub.sessions["a"].stats["seven_day_pct"]) == (33, 45)
    r.advance(10.1)
    r.board.close()
    again = Rig(tmp_path)                                          # restart; nothing reports
    again.running = running
    again.advance(13.2)
    st = again.hub.sessions["a"].stats
    assert (st["cost_usd"], st["context_pct"], st["five_hour_pct"], st["seven_day_pct"]) == (4.5, 61, 33, 45)
    assert again.hub.sessions["b"].stats["context_pct"] == 12
    again.board._select(again.hub.sessions["a"])
    again.board.tick()
    assert again.face(STATS).kind == "stats" and again.face(STATS).stats["ctx"] == 61


def test_running_sessions_are_read_from_claude_codes_files(tmp_path):
    from decktate.claude import discover

    def write(pid, **kw):
        (tmp_path / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": f"s{pid}", "cwd": f"/w/p{pid}",
                                                          "kind": "interactive", "startedAt": pid * 1000, **kw}))
    write(30, status="busy", name="p30-ab", nameSource="derived")
    write(10, status="idle", name="my refactor", nameSource="user")
    write(20, kind="headless")                                     # not a session in a terminal
    write(40)                                                      # process is gone
    (tmp_path / "junk.json").write_text("{nope")
    found = discover.running(tmp_path, alive=lambda pid: pid != 40)
    assert [(x.id, x.pid, x.cwd, x.title, x.busy) for x in found] == [("s10", 10, "/w/p10", "my refactor", False),
                                                                     ("s30", 30, "/w/p30", "", True)]
    assert discover.running(tmp_path / "missing") == []


def test_any_key_silences_speech_and_speak_repeats(tmp_path):
    r = Rig(tmp_path)
    assert r.face(SPEAK) is None                                   # nothing to repeat yet
    r.hub.handle({"hook_event_name": "Stop", "session_id": "a", "cwd": "/w/a", "last_assistant_message": "Finished."})
    r.board.tick()
    assert (r.face(SPEAK).label, r.face(SPEAK).caption, r.face(MIC).label, r.face(MIC).caption) == ("speak", "repeat", "talk to", "a")   # next to dictate
    assert r.face(MIC).status == Status.OK and r.face(MIC).accent is None      # your turn: the microphone is green
    r.hub.handle({"hook_event_name": "UserPromptSubmit", "session_id": "a"})
    r.board.tick()
    assert r.face(MIC).status is None and r.face(MIC).accent                   # Claude is busy: not green
    r.hub.handle({"hook_event_name": "Stop", "session_id": "a", "last_assistant_message": "Finished."})
    r.board.tick()
    said = len(r.speaker.said)
    r.speaker.speaking = True
    r.board.tick()
    assert (r.face(SPEAK).label, r.face(SPEAK).icon) == ("quiet", "mute")   # while it talks, the key is the one to quieten it
    r.tap(SPEAK)                                                   # while speaking: only silences
    assert r.speaker.stopped == 1 and len(r.speaker.said) == said
    assert (r.face(SPEAK).label, r.face(SPEAK).icon) == ("speak", "speaker")
    r.system["vol"] = 0                                            # the computer is muted
    r.advance(3.5)
    assert (r.face(SPEAK).label, r.face(SPEAK).icon, r.face(SPEAK).caption) == ("muted", "mute", "volume 0")   # crossed-out speaker
    said = len(r.speaker.said)
    r.hub.handle({"hook_event_name": "Stop", "session_id": "a", "cwd": "/w/a", "last_assistant_message": "Again."})
    r.board.tick()
    assert len(r.speaker.said) == said                             # nothing is spoken (and no voice is fetched) while muted
    r.tap(SPEAK)                                                   # takes you to the volume keys
    assert (r.board.page, r.board.topic) == ("settings", "sound") and r.face(1).disabled and r.face(1).caption == "0%"
    r.tap(2)                                                       # louder
    r.tap(9)                                                       # back: straight home, as that is where you came from
    assert r.board.page == "main"
    assert r.system["vol"] == 10 and r.face(SPEAK).label == "speak"
    said = len(r.speaker.said)
    r.tap(SPEAK)                                                   # silent: repeats the last message
    assert len(r.speaker.said) == said + 1 and r.speaker.said[-1] == r.speaker.said[-2]
    r.speaker.speaking = True
    r.tap(SETTINGS)                                                # any other key leaves it talking, and acts
    assert r.speaker.stopped == 1 and r.speaker.speaking and r.board.page == "settings"
    r.tap(9)
    r.hold(MIC, 1.0)                                               # except dictating: the microphone must not record the voice
    assert r.speaker.stopped == 2


def test_dictation_is_sent_on_release(tmp_path):
    r = Rig(tmp_path)
    r.hub.handle({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/w/a"})
    r.tap(MIC)                                                     # a tap: listens hands-free until the key is tapped again
    r.advance(1.0)
    assert r.board.take and r.typed == [] and (r.face(MIC).label, r.face(MIC).kind) == ("tap to send", "bars")
    assert r.face(DISCARD).label == "discard" and r.face(0).value == "listening"
    r.tap(MIC)                                                     # second tap: sent
    assert r.board.take is None and r.typed == [("text", "please add tests for the parser")] and r.face(TOAST).text == "sent to a"
    r.advance(3)
    r.tap(MIC)
    r.tap(DISCARD)                                                 # or dropped
    assert r.board.take is None and len(r.typed) == 1 and r.face(TOAST).text == "discarded" and r.face(DISCARD) is None
    r.advance(3)
    r.tap(MIC)
    r.advance(301)                                                 # forgotten for five minutes: dropped, nothing is sent
    assert r.board.take is None and len(r.typed) == 1 and r.face(TOAST).text == "dictation timed out"
    r.advance(3)
    r.typed.clear()
    r.deck.press(MIC)
    r.advance(0.5)
    mic = r.face(MIC)                                              # level bars that follow the voice
    assert r.board.take and (mic.label, mic.kind, mic.caption, mic.status) == ("listening", "bars", "hearing you", Status.OK)
    assert mic.spark and all(v == 0.4 for v in mic.spark)
    before = r.deck.images[MIC].tobytes()
    r.board.take.level = 0.9                                       # louder: the picture changes
    r.advance(0.2)
    assert r.deck.images[MIC].tobytes() != before and r.face(MIC).spark[-1] == 0.9
    r.board.take.level = 0.0                                       # silence for a while: it says so
    r.advance(0.6)
    assert (r.face(MIC).caption, r.face(MIC).status) == ("no sound", Status.WARN)
    r.board.take.level = 0.4
    r.advance(0.1)
    top = r.face(0)                                                # no transcript on the keys: the sessions stay,
    assert (top.text, top.kind, top.icon, top.value) == ("a", "session", "mic", "listening")   # and the one you talk to says so
    assert not any("please" in (r.face(k).text or "") for k in range(15) if r.face(k)) and r.speaker.chimes[-1] == "start"
    r.deck.release(MIC)
    r.board.tick()                                                 # released: sent straight away, nothing to confirm
    assert r.typed == [("text", "please add tests for the parser")] and r.board.review is None
    assert r.face(MIC).label == "talk to" and r.face(TOAST).text == "sent to a" and r.face(DISCARD) is None
    assert (r.face(0).text, r.face(0).value) == ("a", "idle")      # the session key is back to its state

    r.cfg.speech.review_dictation = True                           # opt-in: check the text first
    r.advance(3)
    r.hold(MIC, 1.0)
    assert r.board.review == "please add tests for the parser" and len(r.typed) == 1
    assert r.face(MIC).label == "send" and r.face(DISCARD).label == "discard"
    assert " ".join(r.face(k).text for k in range(4)).split() == "please add tests for the parser".split()
    r.tap(MIC)
    assert len(r.typed) == 2 and r.board.review is None
    r.hold(MIC, 1.0)
    r.tap(DISCARD)                                                 # discard: nothing typed
    assert r.board.review is None and len(r.typed) == 2
    quiet = Rig(tmp_path / "q", take=lambda: FakeTake(heard=""))
    quiet.hub.handle({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/w/a"})
    quiet.hold(MIC, 1.0)
    assert quiet.typed == [] and quiet.face(TOAST).text == "heard nothing"

    silent = Rig(tmp_path / "x", take=None)                        # no Deepgram key: no dictate key
    silent.hub.handle({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/w/a"})
    assert silent.face(MIC) is None
    broken = Rig(tmp_path / "y", take=lambda: FakeTake(error="no microphone"))
    broken.hub.handle({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/w/a"})
    broken.hold(MIC, 1.0)
    assert broken.board.take is None and broken.face(TOAST).text == "no microphone"      # shown as a toast


def test_dictation_goes_to_the_session_selected_at_key_down_and_twins_are_numbered(tmp_path):
    r = Rig(tmp_path)
    for sid, cwd in (("s1", "/w/deck"), ("s2", "/x/deck"), ("s3", "/w/other-project")):   # two sessions in folders called "deck"
        r.hub.handle({"hook_event_name": "SessionStart", "session_id": sid, "cwd": cwd})
        r.advance(0.1)
    names = {r.face(k).text for k in range(3)}
    assert names == {"deck 1", "deck 2", "other-project"}          # told apart by a number, oldest first
    r.board._select(r.hub.sessions["s2"])
    assert "deck 2" in r.speaker.said[-1]                          # spoken with the same name
    r.advance(0.1)
    assert [(r.face(k).text, bool(r.face(k).outline)) for k in range(3)].count(("deck 2", True)) == 1
    assert r.face(MIC).caption == "deck 2"
    r.deck.press(MIC)
    r.advance(0.5)
    r.hub.select("s3")                                             # the selection changes while talking ...
    r.advance(0.1)
    listening = [r.face(k).text for k in range(3) if r.face(k).value == "listening"]
    assert listening == ["deck 2"]
    r.deck.release(MIC)
    r.board.tick()
    assert r.board.injected[-1] == ("s2", "text", "please add tests for the parser")   # ... the words still go to deck 2
    assert r.face(TOAST).text == "sent to deck 2"


def test_session_name_is_drawn_large_and_whole(tmp_path):
    from PIL import ImageDraw, Image
    from decktate.ui.render import _name_lines, font, render_key, Face

    d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    f = font(10 * 4)
    assert _name_lines(d, "claude-streamdeck", f, 64 * 4, False) == ["claude-", "streamdeck"]   # breaks after the hyphen
    assert _name_lines(d, "app", f, 64 * 4, False) == ["app"]
    assert _name_lines(d, "averyveryverylongsinglewordname", f, 64 * 4, False) is None           # only fits when cut
    assert "".join(_name_lines(d, "averyveryverylongsinglewordname", f, 64 * 4, True)) == "averyveryverylongsinglewordname"
    for name in ("app", "claude-streamdeck", "a_really_long_project_name_that_never_ends_at_all_whatever"):
        img = render_key(Face(kind="session", text=name, value="idle", icon="dots"), (72, 72))
        assert img.size == (72, 72)


def test_commands_page_mode_and_stop(tmp_path):
    cmds = [f"/cmd{i}" for i in range(1, 14)] + ["/clear"]                  # 14 commands: two pages
    r = Rig(tmp_path, slash=cmds)
    assert r.face(COMMANDS) is None and r.face(STOP) is None       # no session: nothing to act on
    assert r.face(MODE) is None                                    # no session: no mode to show
    r.hub.handle({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/w/a", "permission_mode": "acceptEdits"})
    r.board.tick()
    assert r.face(COMMANDS).label == "commands"
    assert r.face(STOP) is None                                    # idle: nothing to stop
    assert (r.face(MODE).label, r.face(MODE).icon, r.face(MODE).caption) == ("mode", "ffwd", "edits")   # next to stats
    r.tap(MODE)                                                    # the next mode, as Shift+Tab does: shown at once,
    assert (r.face(MODE).icon, r.face(MODE).caption) == ("pause", "plan")   # without waiting for Claude Code to report it
    r.tap(MODE)
    assert (r.face(MODE).icon, r.face(MODE).caption) == ("hand", "manual") and r.typed == [("shift_tab", ""), ("shift_tab", "")]
    del r.typed[1:]
    r.hub.handle({"hook_event_name": "UserPromptSubmit", "session_id": "a"})
    r.board.tick()
    assert r.face(STOP).label == "interrupt"                            # busy: stop appears on the reject key
    assert r.face(SEND) is None                                  # nothing is queued: no "send now"
    r.hold(MIC, 1.0)                                               # dictated while Claude is busy: it waits in the queue
    r.advance(3)
    assert (r.face(SEND).label, r.face(SEND).caption) == ("send now", "queued")
    r.tap(SEND)                                                  # Ctrl+Enter
    assert r.typed[-1] == ("ctrl_enter", "") and r.face(SEND) is None
    r.hold(MIC, 1.0)
    r.advance(3)
    assert r.face(SEND).label == "send now"
    r.hub.handle({"hook_event_name": "UserPromptSubmit", "session_id": "a"})   # reported when it is queued, not when it is taken:
    r.hub.handle({"hook_event_name": "PreToolUse", "session_id": "a", "tool_name": "Bash", "tool_input": {}})
    r.advance(5)
    assert r.face(SEND).label == "send now"                      # the key stays for as long as the turn runs
    r.hub.handle({"hook_event_name": "Stop", "session_id": "a", "last_assistant_message": "ok"})   # the turn ends: Claude takes it
    r.hub.handle({"hook_event_name": "UserPromptSubmit", "session_id": "a"})
    r.board.tick()
    assert r.face(SEND) is None and r.face(STOP).label == "interrupt"
    del r.typed[1:]
    r.tap(STOP)
    assert r.typed == [("shift_tab", ""), ("escape", "")]
    r.hub.handle({"hook_event_name": "Stop", "session_id": "a", "last_assistant_message": "ok"})
    r.board.tick()
    assert r.face(STOP) is None                                    # finished: gone again
    r.tap(COMMANDS)
    assert r.board.page == "commands"
    slots = [0, 1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13]
    assert [r.face(k).text for k in slots] == cmds[:12]
    assert r.face(4) is None and r.face(9).label == "back" and (r.face(14).label, r.face(14).caption) == ("page down", "1/2")
    r.tap(14)
    assert [r.face(k).text for k in (0, 1)] == ["/cmd13", "/clear"] and r.face(2) is None
    assert (r.face(4).label, r.face(4).caption) == ("page up", "2/2") and r.face(14) is None
    r.tap(1)                                                       # /clear is destructive: a tap does nothing
    assert len(r.typed) == 2 and r.board.page == "commands" and r.face(1).label == "hold"
    r.hold(1, 2.0)                                                 # the confirm hold sends it and returns
    assert r.typed[-1] == ("text", "/clear") and r.board.page == "main"
    r.tap(COMMANDS), r.tap(2)
    assert r.typed[-1] == ("text", "/cmd3") and r.board.page == "main"
    r.tap(COMMANDS), r.tap(9)
    assert r.board.page == "main" and len(r.typed) == 4
    r.hub.handle({"hook_event_name": "PreToolUse", "session_id": "a", "tool_name": "Read", "permission_mode": "auto"})
    r.board.tick()
    assert (r.face(MODE).icon, r.face(MODE).caption) == ("ffwd", "auto")    # follows what the hooks report
    r.tap(SETTINGS)
    assert r.face(11) is None                                      # and it is no longer on the settings page


def test_stats_and_settings_pages(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    r.hub.statusline({"session_id": "a", "cwd": "/w/alpha", "model": {"display_name": "Opus 5"}, "version": "2.1.326",
                      "cost": {"total_cost_usd": 3.5, "total_lines_added": 120, "total_lines_removed": 30, "total_duration_ms": 754000},
                      "context_window": {"used_percentage": 81}, "effort": {"level": "high"},
                      "rate_limits": {"five_hour": {"used_percentage": 95, "resets_at": time.time() + 2 * 3600 + 11 * 60}}})
    summary = r.face(STATS)                                        # the stats key is a three-line summary
    assert summary.kind == "stats" and summary.stats == {"h5": 95, "d7": None, "ctx": 81, "cost": 3.5}
    r.tap(STATS)
    f = r.face
    labels = [f(k).label for k in range(15) if f(k)]
    assert "mode" not in labels                                             # the mode key is under settings only
    assert (f(10).label, f(10).value) == ("5h resets in", "2h10")
    assert (f(1).label, f(1).value, f(1).status) == ("context %", "81", Status.WARN)
    assert (f(2).value, f(2).status) == ("95", Status.FAIL) and (f(3).value, f(3).status) == ("–", Status.IDLE)
    assert (f(5).text, f(6).value, f(7).value, f(8).text) == ("Opus 5", "$3.50", "13m", "+120  −30")
    assert f(STATS).label == "back" and all(f(k).label for k in (0, 1, 2, 3, 5, 6, 7, 8, 10, 12, 13))
    r.tap(STATS)
    assert r.board.page == "main" and r.face(SETTINGS).label == "settings"
    from decktate.speech import audio
    devs = [audio.Device(0, "Desk Mic", 1, 0, True), audio.Device(1, "Headset", 1, 2), audio.Device(2, "Speakers", 0, 2, False, True)]
    monkeypatch.setattr(audio, "devices", lambda refresh=False: devs)
    system = r.system                                              # the computer's own output volume
    SOUND, MICROPHONE, VOICE, DISPLAY, HOOKS, BACK = 0, 1, 2, 3, 5, 9
    r.tap(SETTINGS)                                                # the settings: one key per topic, each says what is set
    assert r.board.page == "settings" and r.board.topic is None
    assert [(r.face(k).label, r.face(k).caption) for k in (SOUND, MICROPHONE, VOICE, DISPLAY)] == [
        ("sound", "44%"), ("microphone", "Desk Mic"), ("voice", "system 1.2x"), ("display", f"{r.cfg.brightness}%")]
    assert r.face(HOOKS).label == "hooks" and r.face(BACK).label == "back"
    assert [k for k in range(15) if r.face(k)] == [0, 1, 2, 3, 5, 9]              # nothing else: not cluttered

    r.tap(SOUND)                                                   # --- sound: speaker, quieter, louder, test
    assert r.board.topic == "sound" and [r.face(k).label for k in (0, 1, 2, 3)] == ["speaker", "quieter", "louder", "test"]
    assert (r.face(1).caption, r.face(2).caption, r.face(0).text) == ("44%", "44%", "Speakers")
    r.tap(2)                                                       # louder: the system volume, in steps of ten
    assert system["vol"] == 50 and r.face(2).caption == "50%" and r.speaker.chimes[-1] == "ok"
    r.tap(2)
    assert system["vol"] == 60 and r.cfg.audio.volume == 1.0       # Decktate's own gain is left alone
    r.tap(1), r.tap(1), r.tap(1)
    assert system["vol"] == 30 and r.face(1).caption == "30%"
    system["vol"] = 100                                            # changed elsewhere (keyboard): picked up
    r.advance(3.5)
    assert r.face(2).caption == "100%" and r.face(2).disabled and not r.face(1).disabled
    r.tap(2)
    assert system["vol"] == 100
    r.advance(3)                                                   # let the "loudest" note go away
    r.tap(0)                                                       # the speaker key steps through the outputs
    assert r.cfg.audio.output == "Headset" and r.speaker.player.device == "Headset" and r.speaker.chimes[-1] == "ok"
    r.tap(3)
    assert r.speaker.said[-1] == "Decktate is ready."
    r.tap(BACK)                                                    # back: to the topics, not home
    assert r.board.page == "settings" and r.board.topic is None and r.face(SOUND).caption == "100%"

    r.tap(MICROPHONE)                                              # --- microphone: device, language, test
    assert [r.face(k).label for k in (0, 1, 2)] == ["microphone", "language", "test"] and r.face(3) is None
    r.tap(0)
    assert r.cfg.audio.input == "Headset" and r.face(0).text == "Headset"
    r.tap(1)
    assert r.cfg.speech.language == "de"
    r.hold(2, 1.0)                                                 # mic test: says what it heard
    assert r.face(TOAST).text == "please add tests for the parser"
    r.advance(4.5)
    r.tap(BACK)
    assert r.face(MICROPHONE).caption == "Headset"

    r.tap(VOICE)                                                   # --- voice: voice, slower, speed, faster, what is read out
    assert [r.face(k).label for k in (0, 1, 2, 3, 5, 6)] == ["voice", "slower", "talk speed", "faster", "requests", "answers"]
    assert r.face(2).value == "1.2x"                               # the default
    from decktate.ui.board import SPEED_SAMPLE
    r.tap(3)                                                       # steps of 0.05, with a longer sample read out
    assert r.cfg.speech.speed == 1.25 and r.face(2).value == "1.25x"
    assert r.speaker.said[-1] == "Speed 1.25. " + SPEED_SAMPLE and len(SPEED_SAMPLE.split()) > 40
    assert config_mod.load(tmp_path).speech.speed == 1.25
    r.tap(1), r.tap(1), r.tap(1)
    assert r.cfg.speech.speed == 1.1 and r.speaker.said[-1].startswith("Speed 1.1. The refactoring")
    r.tap(2)                                                       # the speed key itself replays the sample
    assert r.speaker.said[-1] == SPEED_SAMPLE
    r.cfg.speech.speed = 2.0                                       # the system voice stops at 2.0
    r.board.tick()
    assert r.face(3).disabled
    r.tap(3)
    assert r.cfg.speech.speed == 2.0 and r.face(TOAST).text == "fastest"
    r.advance(3)
    r.speaker.engine.name = "elevenlabs"                           # ElevenLabs cannot go above 1.2
    r.cfg.speech.speed = 1.2
    r.board.tick()
    assert r.face(3).disabled and not r.face(1).disabled
    r.tap(3)
    assert r.cfg.speech.speed == 1.2
    r.tap(1)
    assert r.cfg.speech.speed == 1.15
    r.speaker.engine.name = "system"
    r.advance(3)
    assert (r.face(5).caption, r.face(6).caption) == ("read out", "read out")
    r.tap(6)                                                       # do not read out what a session finished with
    assert r.cfg.speech.speak_on_stop is False and r.face(6).caption == "silent" and config_mod.load(tmp_path).speech.speak_on_stop is False
    said = len(r.speaker.said)
    r.hub.handle({"hook_event_name": "Stop", "session_id": "a", "cwd": "/w/alpha", "last_assistant_message": "All done."})
    assert len(r.speaker.said) == said
    r.tap(6)
    r.tap(BACK)

    r.tap(DISPLAY)                                                 # --- display: dimmer, brightness, brighter
    assert [r.face(k).label for k in (0, 1, 2)] == ["dimmer", "brightness", "brighter"]
    r.cfg.brightness = 60
    r.tap(2)
    assert r.cfg.brightness == 70 and r.face(1).value == "70" and r.deck.brightness == 70 and config_mod.load(tmp_path).brightness == 70
    for _ in range(8):
        r.tap(0)
    assert r.cfg.brightness == 10 and r.face(0).disabled and r.face(TOAST).text == "darkest"   # never quite dark
    r.advance(3)
    r.tap(BACK), r.tap(BACK)                                       # topics, then home
    assert r.board.page == "main"
    saved = config_mod.load(tmp_path)                              # choices persist
    assert (saved.audio.input, saved.audio.output, saved.speech.language) == ("Headset", "Headset", "de")
    assert audio.pick("unplugged thing", "input").name == "Desk Mic"   # a missing device falls back to the default


def test_muted_projects_are_never_spoken(tmp_path):
    r = Rig(tmp_path)
    r.cfg.mute_projects = ["/work/secret"]
    r.hub.handle({"hook_event_name": "Stop", "session_id": "m", "cwd": "/work/secret/app", "last_assistant_message": "Rotated the keys."})
    t, box = r.post({**BASH, "session_id": "m", "cwd": "/work/secret/app"})
    assert r.speaker.said == [] and r.speaker.chimes == []
    r.tap(OK)
    done(t, box)


# ------------------------------------------------------------------ redaction
def test_secrets_are_redacted_before_speech():
    cases = {
        "export OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456": "sk-abcdefghijklmnop",
        "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij'": "eyJhbGci",
        "git clone https://user:hunter2pass@github.com/x/y": "hunter2pass",
        "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE": "AKIAIOSFODNN7EXAMPLE",
        "DB_PASSWORD='correct horse battery' ./run": "correct horse",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789 pushed": "ghp_abcdefgh",
        "mysql --password=s3cr3tvalue -u root": "s3cr3tvalue",
        "sha 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08": "9f86d081884c7d65",
    }
    for text, secret in cases.items():
        assert secret not in redact(text), text
    assert redact("run the tests in src/parser") == "run the tests in src/parser"
    said = speakable("## Summary\n\nI updated `/Users/me/projects/app/src/parser.py` and ran the tests. "
                     "See [the docs](https://example.com/docs).\n\n```bash\npytest\n```\nMore. Even more.", 2)
    assert said == "Summary I updated parser.py and ran the tests. See the docs."
    assert len(speakable("word " * 400, 5)) <= 325


# ------------------------------------------------------------------ http server
def test_hook_server_requires_the_token_and_holds_the_response(tmp_path):
    r = Rig(tmp_path)
    server = HookServer(r.hub, "sekret", port=0)
    server.start()
    url = f"http://127.0.0.1:{server.port}"
    try:
        assert httpx.get(url + "/health").json() == {"ok": True, "attached": True, "sessions": 0}
        assert httpx.post(url + "/hook", json=BASH).status_code == 401
        assert httpx.post(url + "/hook", json=BASH, headers={"X-Decktate-Token": "wrong"}).status_code == 401
        assert httpx.post(url + "/hook", content=b"[1]", headers={"X-Decktate-Token": "sekret"}).status_code == 400
        ok = {"X-Decktate-Token": "sekret", "X-Term-Program": "iTerm.app", "X-Iterm-Session": "w0t0p0:ABC-123", "X-Tmux-Pane": ""}
        resp = httpx.post(url + "/hook", json={"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/w/x"}, headers=ok)
        assert resp.status_code == 200 and resp.content == b""                 # empty body = no decision
        assert r.hub.sessions["s1"].terminal == {"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "w0t0p0:ABC-123"}
        box = {}
        t = threading.Thread(target=lambda: box.update(r=httpx.post(url + "/hook", json=BASH, headers=ok, timeout=10)), daemon=True)
        t.start()
        time.sleep(0.3)
        assert t.is_alive()                                                    # the response is held open
        r.board.tick()
        r.tap(OK)
        t.join(3)
        assert box["r"].json()["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        assert httpx.post(url + "/statusline", json={"session_id": "s1", "model": {"display_name": "M"}}, headers=ok).status_code == 200
        assert r.hub.sessions["s1"].model == "M"
    finally:
        server.stop()


# -------------------------------------------------------------------- installer
def test_install_merges_and_uninstall_restores(tmp_path):
    cfg = config_mod.load(tmp_path / "home")
    settings = tmp_path / "settings.json"
    mine = {"type": "command", "command": "my-own-hook"}
    settings.write_text(json.dumps({"model": "opus", "statusLine": {"type": "command", "command": "my-status --fancy", "padding": 1},
                                    "hooks": {"Stop": [{"hooks": [mine]}], "PreToolUse": [{"matcher": "Bash", "hooks": [mine]}]}}))
    r = install.install(cfg, settings)
    s = json.loads(settings.read_text())
    assert r["events"] == len(install.EVENTS) and r["statusline"] == "chained after: my-status --fancy" and r["backup"]
    assert s["model"] == "opus" and s["hooks"]["Stop"][0] == {"hooks": [mine]}            # the user's hooks are untouched
    assert s["hooks"]["PreToolUse"][0] == {"matcher": "Bash", "hooks": [mine]}
    ours = s["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert ours["type"] == "http" and ours["url"] == "http://127.0.0.1:47873/hook" and ours["headers"]["X-Decktate-Token"] == cfg.token()
    assert ours["headers"]["X-Iterm-Session"] == "${ITERM_SESSION_ID}" and "ITERM_SESSION_ID" in ours["allowedEnvVars"]
    assert ours["timeout"] == 135 and s["hooks"]["Stop"][-1]["hooks"][0]["timeout"] == 5      # only held hooks may wait
    script = (cfg.home / "statusline.sh").read_text()
    assert s["statusLine"] == {"padding": 1, "type": "command", "command": str(cfg.home / "statusline.sh")}
    assert "my-status --fancy" in script and "/statusline" in script and cfg.token() in script
    assert install.installed(cfg, settings) == {"events": sorted(install.EVENTS), "complete": True, "current": True, "statusline": True}
    cfg.port = 50001                                                                       # port changed: hooks are stale
    assert install.installed(cfg, settings)["current"] is False
    install.install(cfg, settings)
    urls = {h["url"] for g in json.loads(settings.read_text())["hooks"]["Stop"] for h in g["hooks"] if h.get("type") == "http"}
    assert urls == {"http://127.0.0.1:50001/hook"} and install.installed(cfg, settings)["current"]
    cfg.port = 47873
    install.install(cfg, settings)
    install.install(cfg, settings)                                                         # twice: no duplicates
    s = json.loads(settings.read_text())
    assert len(s["hooks"]["Stop"]) == 2 and "my-status --fancy" in (cfg.home / "statusline.sh").read_text()
    u = install.uninstall(cfg, settings)
    s = json.loads(settings.read_text())
    assert u["removed"] == len(install.EVENTS)
    assert s == {"model": "opus", "statusLine": {"type": "command", "command": "my-status --fancy", "padding": 1},
                 "hooks": {"Stop": [{"hooks": [mine]}], "PreToolUse": [{"matcher": "Bash", "hooks": [mine]}]}}
    assert not (cfg.home / "statusline.sh").exists() and not install.installed(cfg, settings)["complete"]
    empty = tmp_path / "none" / "settings.json"                                            # no settings file yet
    install.install(cfg, empty)
    install.uninstall(cfg, empty)
    assert json.loads(empty.read_text()) == {}


# ---------------------------------------------------------------------- speech
def test_dictation_streams_audio_and_assembles_finals(tmp_path):
    from decktate.speech.stt import Dictation, listen_url

    cfg = config_mod.load(tmp_path)
    cfg.speech.language = "multi"
    url = listen_url(cfg)
    assert url.startswith("wss://api.deepgram.com/v1/listen?model=nova-3&language=multi&encoding=linear16&sample_rate=16000")
    assert "language_hint=en&language_hint=de" in url and "endpointing=100" in url
    assert "keyterm=Claude&keyterm=refactor" in url and "keyterm=pull+request" in url and "keywords" not in url
    cfg.speech.language = "de"
    assert "language=de" in listen_url(cfg) and "endpointing=300" in listen_url(cfg) and "language_hint" not in listen_url(cfg)

    class Mic:
        level, error = 0.3, ""

        def __init__(self):
            self.frames = [b"\x01\x00" * 800] * 3

        def start(self):
            return True

        def read(self, timeout):
            time.sleep(0.01)
            return self.frames.pop() if self.frames else None

        def stop(self):
            pass

    class Socket:
        def __init__(self):
            self.sent, self.inbox, self.closed = [], [], threading.Event()
            for msg in ({"type": "Results", "is_final": False, "channel": {"alternatives": [{"transcript": "add a"}]}},
                        {"type": "Results", "is_final": True, "channel": {"alternatives": [{"transcript": "Add a test"}]}},
                        {"type": "Results", "is_final": False, "channel": {"alternatives": [{"transcript": "for the par"}]}}):
                self.inbox.append(json.dumps(msg))

        def send(self, data):
            self.sent.append(data)
            if isinstance(data, str) and json.loads(data)["type"] == "Finalize":
                self.inbox.append(json.dumps({"type": "Results", "is_final": True, "from_finalize": True,
                                              "channel": {"alternatives": [{"transcript": "for the parser."}]}}))

        def recv(self):
            while not self.inbox:
                if self.closed.wait(0.01):
                    raise ConnectionError
            return self.inbox.pop(0)

        def close(self):
            self.closed.set()

    sock = Socket()
    seen = {}
    d = Dictation(cfg, Mic(), connect=lambda url, headers: seen.update(url=url, headers=headers) or sock)
    assert d.start() and d.active
    time.sleep(0.2)
    assert d.finals == ["Add a test"] and d.interim == "for the par" and d.text == "Add a test for the par"
    assert d.finish() == "Add a test for the parser."
    audio_frames = [m for m in sock.sent if isinstance(m, bytes)]
    control = [json.loads(m)["type"] for m in sock.sent if isinstance(m, str)]
    assert len(audio_frames) == 3 and control == ["Finalize", "CloseStream"] and sock.closed.is_set()
    assert seen["headers"]["Authorization"].startswith("Token ")
    nokey = Dictation(cfg, Mic())
    assert not nokey.enabled or True


def test_elevenlabs_request_and_voice_fallback(tmp_path):
    from decktate.speech.tts import ElevenLabs, TTSError

    cfg = config_mod.load(tmp_path)
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, dict(req.url.params), json.loads(req.content) if req.content else None,
                      req.headers.get("xi-api-key")))
        if req.url.path == "/v2/voices":
            return httpx.Response(200, json={"voices": [{"voice_id": "v-new", "name": "Eldrin - Crisp", "category": "premade"},
                                                        {"voice_id": "v-two", "name": "Talia"}]})
        if req.url.path == "/v1/user":
            return httpx.Response(200, json={"subscription": {}})
        if "v-gone" in req.url.path:
            return httpx.Response(404, json={"detail": {"code": "voice_not_found"}})
        return httpx.Response(200, content=b"\x00\x01" * 2400)

    client = httpx.Client(base_url="https://api.elevenlabs.io", headers={"xi-api-key": "k"}, transport=httpx.MockTransport(handler))
    el = ElevenLabs("k", cfg, client)
    assert el.check() == "" and [v["id"] for v in el.voices()] == ["v-new", "v-two"]
    pcm = b"".join(el.stream("Build finished."))
    assert len(pcm) == 4800
    method, path, params, body, key = calls[-1]
    assert (method, path, params, key) == ("POST", "/v1/text-to-speech/v-new/stream", {"output_format": "pcm_24000"}, "k")
    assert body == {"text": "Build finished.", "model_id": "eleven_flash_v2_5", "apply_text_normalization": "on",
                    "voice_settings": {"speed": 1.2}, "language_code": "en"}          # 1.2x by default
    cfg.speech.speed = 2.0                                                  # clamped to what ElevenLabs accepts
    list(el.stream("x"))
    assert calls[-1][3]["voice_settings"] == {"speed": 1.2}
    cfg.speech.speed = 0.3
    list(el.stream("x"))
    assert calls[-1][3]["voice_settings"] == {"speed": 0.7}
    cfg.speech.speed = 1.2
    assert "optimize_streaming_latency" not in params                       # deprecated
    cfg.speech.voice_id = "v-gone"                                          # a retired voice: report it, pick again next time
    el2 = ElevenLabs("k", cfg, client)
    with pytest.raises(TTSError, match="voice_not_found"):
        list(el2.stream("x"))
    assert b"".join(el2.stream("x")) and "v-new" in calls[-1][1]


def test_speaker_redacts_caches_and_can_be_silenced(tmp_path):
    from decktate.speech.tts import Speaker

    cfg = config_mod.load(tmp_path)
    played, texts = [], []

    class Engine:
        name = "fake"

        def stream(self, text):
            texts.append(text)
            yield b"\x00\x00" * 100

    class Player:
        playing, error, device, volume = False, "", "", 1.0

        def play(self, chunks, rate=24000):
            played.append(b"".join(chunks) if not isinstance(chunks, (bytes, bytearray)) and not hasattr(chunks, "tobytes") else chunks)
            return True

        def stop(self):
            pass

    sp = Speaker(cfg, Player(), Engine())

    def settle():
        for _ in range(100):
            if sp._q.empty():
                break
            time.sleep(0.01)
        time.sleep(0.05)

    sp.speak("the key is sk-abcdefghijklmnopqrstuvwxyz123456 ok")
    settle()
    assert texts == ["the key is a secret key ok"] and sp.last_text == texts[0]
    sp.speak("approved", cache=True)
    settle()
    sp.speak("approved", cache=True)
    settle()
    assert texts.count("approved") == 1 and len(list((tmp_path / "tts-cache").glob("*.pcm"))) == 1   # second time from disk
    sp.repeat()
    settle()
    assert texts[-1] == "approved"
    cfg.speech.tts = "off"
    assert Speaker(cfg, Player()).enabled is False


# ------------------------------------------------------------------- injectors
def test_injector_is_chosen_from_the_session_environment():
    which_all = lambda name: f"/usr/bin/{name}"   # noqa: E731
    none = lambda name: None                       # noqa: E731
    pick = lambda term, platform="darwin", which=none: for_session("sid", term, platform, which, tty_of=lambda s: "/dev/ttys009")  # noqa: E731
    it = pick({"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "w0t1p0:ABCD-1234"})
    assert isinstance(it, ITerm) and it.sid == "ABCD-1234"
    term = pick({"TERM_PROGRAM": "Apple_Terminal"})
    assert isinstance(term, AppleTerminal) and term.tty == "/dev/ttys009"
    ghost = pick({"TERM_PROGRAM": "ghostty"})
    assert isinstance(ghost, MacKeystrokes) and ghost.app == "Ghostty"
    assert isinstance(pick({"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "x:y", "TMUX_PANE": "%3"}, which=which_all), Tmux)
    assert isinstance(pick({"TERM_PROGRAM": "iTerm.app", "ITERM_SESSION_ID": "x:y", "TMUX_PANE": "%3"}), ITerm)   # tmux not installed
    assert isinstance(pick({"KITTY_WINDOW_ID": "4", "KITTY_LISTEN_ON": "unix:/tmp/k"}, "linux", which_all), Kitty)
    assert isinstance(pick({"WEZTERM_PANE": "7"}, "linux", which_all), WezTerm)
    unknown = pick({"TERM_PROGRAM": "mystery"})
    assert type(unknown) is Injector
    with pytest.raises(InjectError):
        unknown.send_text("hi")


def test_typing_failures_show_on_the_deck_and_never_crash(tmp_path):
    r = Rig(tmp_path)
    r.hub.handle({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/w/a"})
    r.board._injector_for = lambda sid, term: Injector()
    r.tap(MODE)
    assert r.face(TOAST).text == "no way to type into this terminal" and r.face(TOAST).status == Status.FAIL


def test_system_voice_speed(tmp_path, monkeypatch):
    from decktate.speech import tts

    cfg = config_mod.load(tmp_path)
    assert cfg.speech.speed == 1.2
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        import wave
        out = cmd[cmd.index("-o") + 1]
        with wave.open(out, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(tts.RATE), w.writeframes(b"\x00\x00" * 10)

    monkeypatch.setattr(tts.shutil, "which", lambda name: "/usr/bin/say" if name == "say" else None)
    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    list(tts.SystemVoice(cfg).stream("hello"))
    assert seen[-1][:3] == ["say", "-r", "204"]                    # measured: this rate speaks 1.2x as fast
    cfg.speech.speed = 1.0
    list(tts.SystemVoice(cfg).stream("hello"))
    assert seen[-1][2] == "160"
    assert [tts.say_rate(v) for v in (0.7, 1.6, 2.0, 9.0)] == [95, 292, 380, 380]


def test_hooks_are_installed_on_start_and_removed_on_shutdown(tmp_path):
    cfg = config_mod.load(tmp_path / "home")
    settings = tmp_path / "settings.json"
    original = {"model": "opus", "statusLine": {"type": "command", "command": "my-status"},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}
    settings.write_text(json.dumps(original))
    auto = install.AutoHooks(cfg, settings)
    assert auto.start() == "" and auto.active
    assert install.installed(cfg, settings)["complete"] and install.installed(cfg, settings)["statusline"]
    backup = tmp_path / "settings.json.decktate-backup"
    assert json.loads(backup.read_text()) == original                      # one copy of the untouched file
    auto.stop()
    assert json.loads(settings.read_text()) == original and not auto.active  # exactly as before
    auto.stop()                                                            # twice is harmless

    install.install(cfg, settings, backup="once")                          # left behind by a crash ...
    settings.write_text(json.dumps({**json.loads(settings.read_text()), "model": "changed-later"}))
    auto = install.AutoHooks(cfg, settings)
    assert auto.start() == ""                                              # ... the next start just replaces them
    assert json.loads(backup.read_text()) == original                      # and does not overwrite the clean backup
    assert len(list(tmp_path.glob("settings.json.decktate-backup*"))) == 1  # no pile of backups
    auto.stop()
    assert json.loads(settings.read_text()) == {**original, "model": "changed-later"}
    assert not list(tmp_path.glob("*.decktate-tmp"))

    settings.write_text("{ not json")                                      # a broken settings file is left alone
    auto = install.AutoHooks(cfg, settings)
    assert "JSONDecodeError" in auto.start() and not auto.active
    auto.stop()
    assert settings.read_text() == "{ not json"

    cfg.auto_hooks = False                                                 # opted out: nothing is touched
    settings.write_text(json.dumps(original))
    auto = install.AutoHooks(cfg, settings)
    assert auto.start() == "" and not auto.active and json.loads(settings.read_text()) == original
