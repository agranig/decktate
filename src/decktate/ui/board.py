"""The board: what each of the 15 keys shows and does.

Main page (a key with nothing to do right now is blank):

    [ S1  ] [ S2      ] [ S3            ] [ +N more  ] [ SETTINGS ]
    [ NO/STOP ] [ OK / SEND NOW ] [ ALWAYS  ] [ MODE     ] [ STATS    ]
    [ MIC     ] [ SPEAK         ] [ discard ] [ result   ] [  /       ]

The permission mode lives on the settings page. Grammar: tap = act. Hold = push-to-talk on MIC and NO, an outline that runs
around the key to confirm on ALWAYS (and /clear). Any key press silences speech.
In settings the right column is: faster, back, slower (talk speed).
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..backend.base import DeckBackend
from ..claude import discover as discover_mod
from ..config import Config
from ..core.hub import Hub, Session, State, describe
from ..core.redact import speakable
from ..core.hub import LIMIT_KEYS
from ..inject import InjectError, for_session, read_screen, screen_text
from ..status import Status
from .render import ACCENT, COLORS, MUTED, TEXT, Face, render_key, snake

PTT_MS = 220  # the reject key: hold this long before push-to-talk starts (shorter than a confirm hold)
TAP_MS = 500  # the mic key: a press shorter than this is a tap (talk hands-free, tap again to send), longer is hold-to-talk
BUSY = (State.WORKING, State.PERMISSION, State.QUESTION)  # Claude is in a turn: new text is queued
DETACHED_KEEP_DAYS = 30  # how long a detached session is remembered
TALK_COMMANDS = ("/btw",)  # slash commands that take what you say: /btw asks a side question while Claude keeps working
QUEUE_GUESS_S = 45  # without a readable screen: how long "send now" is offered after the deck queued text
RUNNING_KEY = 13  # shells, monitors and subagents of the selected session (a result note takes its place for a moment)
DISCARD_KEY = 12  # always here: one key away from the mic key, so that a slip of the finger does not drop a dictation
RETURN_S = 4  # a session that ends and starts again in the same terminal within this time keeps its key and stays selected
LATCH_MAX_S = 300  # a dictation started with a tap is dropped if nobody ends it for this long
SPEAK_KEY = 11
TOAST_KEY = 13  # where short results ("approved", "sent", errors) appear
SPEED_STEP = 0.05
# Read out after every speed change: long enough to judge the pace, and typical of what gets spoken.
SPEED_SAMPLE = ("The refactoring is finished and all forty two tests pass. I moved the session handling into its own "
                "module, updated the three call sites, and added a test for expired tokens. "
                "One thing needs your decision: the old config format is still accepted, should I remove it?")
# pictogram, word, colour - the pictograms are the ones Claude Code shows under its prompt (two triangles, two bars)
MODES = {"default": ("hand", "manual", ACCENT), "acceptEdits": ("ffwd", "edits", ACCENT), "plan": ("pause", "plan", (45, 212, 191)),
         "auto": ("ffwd", "auto", COLORS[Status.WARN]), "dontAsk": ("ffwd", "don't ask", COLORS[Status.WARN]),
         "bypassPermissions": ("ffwd", "bypass", COLORS[Status.FAIL])}
MODE_CYCLE = ("default", "acceptEdits", "plan", "bypassPermissions", "auto")  # the order Shift+Tab goes through them
STATE_FACE = {State.IDLE: ("dots", "idle"), State.WORKING: ("running", "working"), State.PERMISSION: ("shield", "approve?"),
              State.QUESTION: ("question", "question"), State.WAITING: ("mic", "your turn"), State.DONE: ("check", "done"),
              State.ERROR: ("fail", "error")}


def _overlap(spoken: str, visible: str, least: int = 12) -> int:
    """How much of the start of `visible` was already spoken: the longest beginning of it that `spoken` ends
    with (so also all of `spoken`, if `visible` starts with it). 0 if that is shorter than `least` characters,
    which is too little to be sure."""
    if visible.startswith(spoken):
        return len(spoken)
    for n in range(min(len(spoken), len(visible)), least - 1, -1):
        if spoken.endswith(visible[:n]):
            return n
    return 0


def place(s: Session) -> str:
    """Where a session runs: its terminal tab, else its process. The same across /clear and /resume,
    which end one session and start another in the same place."""
    t = s.terminal
    return (t.get("ITERM_SESSION_ID") or t.get("TERM_SESSION_ID") or t.get("TMUX_PANE") or t.get("KITTY_WINDOW_ID")
            or t.get("WEZTERM_PANE") or t.get("TTY") or (f"pid {s.pid}" if s.pid else ""))


@dataclass
class Key:
    face: Face | None = None
    tap: Callable[[], None] | None = None
    hold: Callable[[], None] | None = None  # fires once after the hold time
    confirm: Callable[[], None] | None = None  # hold with the outline; fires when it closes
    talk: tuple[Callable[[], None], Callable[[], None]] | None = None  # push to talk: (start, finish)
    colour: tuple = TEXT


def money(v) -> str:
    return "–" if v is None else f"${v:.2f}" if v < 100 else f"${v:.0f}"


def until(ts) -> str:
    """Time left until a Unix timestamp: 2h10, 45m, now."""
    if not ts:
        return "–"
    left = ts - time.time()
    if left <= 0:
        return "now"
    return f"{int(left // 3600)}h{int(left % 3600 // 60):02d}" if left >= 3600 else f"{max(1, int(left // 60))}m"


def duration(ms) -> str:
    if not ms:
        return "–"
    s = ms / 1000
    return f"{s / 3600:.1f}h" if s >= 3600 else f"{s / 60:.0f}m" if s >= 60 else f"{s:.0f}s"


class Board:
    def __init__(self, deck: DeckBackend, hub: Hub, cfg: Config, speaker=None, dictation: Callable | None = None,
                 injector_for=for_session, clock: Callable[[], float] = time.monotonic, sync: bool = False,
                 discover: Callable[[], list] | None = None, quit_session: Callable[[int], bool] | None = None,
                 terminal_env: Callable[[int], dict] | None = None, alive: Callable[[int], bool] | None = None, sysvol=None,
                 screen_of: Callable[[str, dict], str | None] | None = None):
        self._alive = alive or discover_mod.alive
        self._screen_of = screen_of or screen_text  # (session id, terminal) -> the text on its screen, or None
        self._screen_queued: set[str] = set()  # sessions whose screen says a message is waiting to be sent
        self._log_lock = threading.Lock()
        self._seen_before: dict[str, dict] = {}  # debugging: the last screen reading that was logged per session
        self._slots_logged: list = []
        hub.trace = lambda line: self.note(line, debug=True)
        self._btw_seen: dict[str, dict] = {}  # session -> the /btw answer on its screen: text, how often seen unchanged, what was spoken
        self._background: dict[str, dict] = {}  # session -> what its screen says runs in the background: {"shells": 2, ...}
        self._watch_now = threading.Event()
        self._watch_first: str | None = None
        self._closed = False
        self._stats_saved: dict = {"limits": {}, "sessions": {}}
        self._stats_written = -1e9
        if sysvol is None:
            from ..speech import sysvol
        self._sysvol = sysvol  # get() / set(percent): the computer's own output volume
        self._discover = discover or discover_mod.running        # () -> the Claude Code sessions running on this computer
        self._quit = quit_session or discover_mod.quit
        self._terminal_env = terminal_env or discover_mod.terminal_env
        self.running: list = []  # what _discover found last
        self._looked = -1e9  # when
        self.menu_for: str | None = None  # the session whose menu (page "session") is open
        self.menu_from = "main"  # the page the menu was opened from
        self.topic: str | None = None  # the settings topic that is open: sound | microphone | voice | display
        self._topic_direct = False  # it was opened from outside the settings (the muted key)
        self._quitting: set[int] = set()  # processes asked to quit
        self._modes: set[str] = {"default", "acceptEdits", "plan"}  # modes Shift+Tab goes through: these, plus any seen
        self._no_return: set[str] = set()  # sessions quit from the deck: their key is free at once
        self._queued: dict[str, tuple[int, float]] = {}  # text from the deck sent while Claude was busy: (turns ended then, when)
        self._screen_read: dict[str, float] = {}  # session -> when its screen was last read successfully
        self._detached_at: dict[str, float] = {}  # session id -> when it was detached (kept in <home>/detached.json)
        self.deck, self.hub, self.cfg, self.speaker, self.clock = deck, hub, cfg, speaker, clock
        self._new_take = dictation  # () -> Dictation
        self._injector_for = injector_for
        self.sync = sync  # run terminal typing inline (tests) instead of on a thread
        self.page = "main"  # main | stats | settings | sessions | session | commands
        self.page_no = 0  # page within the sessions / commands lists
        self.take = None  # the dictation in progress
        self.take_for = ""  # prompt | reason | test
        self.take_target: str | None = None  # the session a dictated prompt goes to: fixed when the key goes down
        self._take_ready = False  # the "go" chime was played
        self._latched: float | None = None  # dictation started with a tap: it runs until the key is tapped again (since when)
        self._slots: list[str | None] = [None, None, None]  # which session is on which session key
        self._slot_place: list[str] = ["", "", ""]  # the terminal tab of the session on each key
        self._slot_last: list[str | None] = [None, None, None]  # the session that had each key last
        self._layout_saved: dict | None = None
        self._slot_freed: list[float] = [-1e9, -1e9, -1e9]  # when a key lost its session
        self._chosen: tuple[str | None, str, float] = (None, "", 0.0)  # the session you talk to: id, terminal tab, missing since
        self._sending = ""  # a released take is being turned into text: its purpose
        self.review: str | None = None  # dictated text waiting to be sent or discarded
        self.menu: dict = {}  # progress through the questions of the pending menu
        self.injected: list[tuple[str, str, str]] = []  # (session, what, payload), for tests and the log
        self._toast: tuple[str, Status, float] | None = None
        self._events: queue.Queue = queue.Queue()
        self._down: dict | None = None
        self._sent: list[bytes | None] = [None] * deck.key_count
        self._voices: list[dict] | None = None
        self._levels: list[float] = []  # recent microphone levels, one per tick, while listening
        self._volume: tuple[float, int | None] | None = None  # (read at, system volume in percent)
        deck.set_brightness(cfg.brightness)
        deck.on_key(lambda key, pressed: self._events.put((key, pressed)))
        self._load_detached()
        self._load_layout()
        self._load_stats()
        hub.subscribe(self._on_event)
        if not sync:
            threading.Thread(target=self._watch_loop, daemon=True, name="decktate-watch").start()
        hub.set_attached(True)

    # -- speech --------------------------------------------------------------
    def _muted(self, s: Session) -> bool:
        return any(s.cwd.startswith(p.rstrip("/")) for p in self.cfg.mute_projects if p)

    def system_volume(self) -> int | None:
        """The computer's output volume in percent; re-read every few seconds, as it may be changed elsewhere."""
        if self._volume is None or self.clock() - self._volume[0] > 3:
            try:
                self._volume = (self.clock(), self._sysvol.get())
            except Exception:
                self._volume = (self.clock(), None)
        return self._volume[1]

    @property
    def muted(self) -> bool:
        """The computer's volume is at zero: nothing is spoken (and no voice is fetched for nothing)."""
        return self.system_volume() == 0

    def say(self, text: str, cache: bool = False) -> None:
        if self.speaker and not self.muted:
            self.speaker.speak(text, cache=cache)

    def name_of(self, s: Session) -> str:
        """The name shown and spoken for a session. Sessions in the same folder would all be called
        the same, so those are numbered in the order they were started: "deck 1", "deck 2"."""
        return self._numbered(s.id, s.name)

    def _numbered(self, session_id: str, name: str) -> str:
        """`name`, with a number if several running sessions (attached or not) share it: numbered by start
        time, so a session keeps its number when it is attached or detached."""
        import os

        same = [(x.started, x.id) for x in self.hub.ordered() if x.name == name]
        same += [(r.started, r.id) for r in self.unattached() if (r.title or os.path.basename(r.cwd.rstrip("/"))) == name]
        ids = [i for _, i in sorted(same)]
        return f"{name} {ids.index(session_id) + 1}" if len(ids) > 1 and session_id in ids else name

    def describe_session(self, s: Session) -> str:
        """What a session needs, or what it finished, as one or two spoken sentences."""
        name = self.name_of(s)
        p = s.pending
        if s.state == State.PERMISSION:
            what = describe(p.tool_name, p.tool_input) if p else (s.notice or "permission")
            return f"{name} asks permission: {speakable(what, 1, 160)}"
        if s.state == State.QUESTION and p and p.questions:
            q = p.questions[self._question_index(s)]
            options = ". ".join(f"{i + 1}: {o.get('label', '')}" for i, o in enumerate(q.get("options") or []))
            return f"{name} asks: {speakable(q.get('question', ''), 2, 200)} {options}."
        if s.state == State.ERROR:
            return f"{name} stopped with an error. {speakable(s.last_message, 1)}"
        if s.state in (State.DONE, State.WAITING) or (s.state == State.IDLE and s.last_message):
            return f"{name} finished. {speakable(s.last_message)}" if s.last_message else f"{name} is waiting for you."
        if s.state == State.WORKING:
            return f"{name} is working: {s.activity or 'thinking'}."
        return f"{name} is idle."

    def _on_event(self, event: str, s: Session) -> None:
        """Called from the hook thread when a session needs attention."""
        if not self.speaker or self._muted(s) or self.muted:
            return
        if event == "permission" and self.cfg.speech.speak_permissions:
            self.speaker.chime("alert")
            self.speaker.speak(self.describe_session(s))
        elif event == "question" and self.cfg.speech.speak_permissions:
            self.speaker.chime("alert")
            self.speaker.speak(self.describe_session(s))
        elif event in ("done", "error") and self.cfg.speech.speak_on_stop:
            self.speaker.speak(self.describe_session(s))
        elif event == "waiting":
            self.speaker.chime("tick")

    # -- actions -------------------------------------------------------------
    def note(self, what: str, debug: bool = False) -> None:
        """One line in <home>/activity.log about something done on the deck: which session was selected, a
        dictation started or ended, what was typed where and whether that worked. It is there to find out
        afterwards why something did not arrive. No dictated or typed text is written, only its length.
        debug=True: only written at log level "debug" (every key, hook event, screen reading, key assignment)."""
        if debug and self.cfg.log_level != "debug":
            return
        try:
            path = self.cfg.home / "activity.log"
            with self._log_lock:  # several threads write: checking the size, starting over and appending are one step
                if path.exists() and path.stat().st_size > 2_000_000:  # keep it small: start over
                    path.unlink()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as f:
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
                    f.write(f"{stamp} {'  ' if debug else ''}{what}\n")
        except OSError:
            pass

    def toast(self, text: str, status: Status = Status.OK, seconds: float = 2.2, logged: str | None = None) -> None:
        """logged: what goes into the activity log instead, when the note itself is something that was said."""
        self.note(f"shown: {text!r} ({status.value})" if logged is None else f"shown: {logged} ({status.value})")
        self._toast = (text, status, self.clock() + seconds)

    def _type(self, what: str, payload: str = "", session: str | None = None) -> None:
        """Type into a session's terminal (the selected one unless given): what = text | escape | shift_tab | enter."""
        s = self.hub.sessions.get(session) if session else self.hub.current()
        if not s:
            return self.toast("no session", Status.FAIL)
        self.injected.append((s.id, what, payload))
        where = f"{self.name_of(s)} [{s.id[:8]}]"
        if what == "text" and s.state in BUSY and payload.split(" ", 1)[0] not in TALK_COMMANDS:
            self._queued[s.id] = (s.turns, self.clock())  # Claude is busy: the text waits in its queue
            if not self.sync:  # once it is typed, look at the screen whether it really is queued
                threading.Timer(1.2, self.watch_now, args=(s.id,)).start()
        elif what in ("ctrl_enter", "escape"):
            self._queued.pop(s.id, None)
            self._screen_queued.discard(s.id)

        def run():
            try:
                inj = self._injector_for(s.id, s.terminal)
                self.note(f"typing {what}" + (f" ({len(payload)} characters)" if what == "text" else "")
                          + f" into {where} via {inj.name}")
                if what == "text" and self._side_view_open(s):
                    # a /btw answer is on the screen: it would swallow the first letters. Close it first.
                    inj.key("escape")
                    self._btw_seen.pop(s.id, None)
                    if not self.sync:
                        time.sleep(0.4)
                inj.send_text(payload) if what == "text" else inj.key(what)
                self.note(f"typed {what} into {where}")
            except InjectError as exc:
                self.toast(str(exc), Status.FAIL, 4)
            except Exception as exc:
                self.toast(type(exc).__name__, Status.FAIL, 4)

        run() if self.sync else threading.Thread(target=run, daemon=True).start()

    def _decide(self, verdict: str, message: str = "") -> None:
        s = self.hub.current()
        if s and self.hub.decide(s.id, verdict, message):
            if self.speaker:
                self.speaker.chime("no" if verdict == "deny" else "ok")
            self.toast({"allow": "approved", "always": "always", "deny": "rejected"}[verdict],
                       Status.FAIL if verdict == "deny" else Status.OK)

    def _select(self, s: Session) -> None:
        text = self.describe_session(s)  # before select() clears the "done" state
        self.hub.select(s.id)
        self.note(f"selected {self.name_of(s)} [{s.id[:8]}]")
        self.watch_now(s.id)  # what is its mode right now?
        self.say(text)

    # -- which sessions ------------------------------------------------------
    def _look(self, now: float, force: bool = False) -> None:
        """Every few seconds: which Claude Code sessions run on this computer. Sessions whose process
        has gone are dropped; attached ones that never sent a hook get idle/busy from Claude Code's own file."""
        if not force and now - self._looked < 3:
            return
        self._looked = now
        try:
            self.running = list(self._discover())
        except Exception:
            return
        by_id = {r.id: r for r in self.running}
        for r in self.running:
            # every session running here is looked after, unless you detached it. (It does not have to send a
            # hook first: after a restart of Decktate the idle ones would otherwise be missing from the keys.)
            if r.id not in self.hub.sessions and r.id not in self.hub.detached and r.pid not in self._quitting:
                self.hub.attach(r.id, cwd=r.cwd, title=r.title, pid=r.pid, terminal=self._terminal_env(r.pid),
                                started=r.started or None)
        for s in self.hub.ordered():
            r = by_id.get(s.id)
            if r:
                s.pid, s.title, s.started = r.pid, s.title or r.title, r.started or s.started
                if not s.heard and not s.pending:
                    s.state, s.activity = (State.WORKING, "busy") if r.busy else (State.IDLE, "")
            elif s.pid and not self._alive(s.pid):
                # its process is gone (closed without telling us). Only that counts: a session that is merely
                # missing from the list for a moment must keep its key and stay selected.
                self.hub.forget(s.id)

    # -- what only the terminal's screen tells ---------------------------------
    def _watch(self, s: Session) -> None:
        """Read the session's terminal screen: the permission mode (a switch made in the terminal fires no hook)
        and whether a message waits in Claude's queue. Takes a few hundred ms; runs on the watcher thread."""
        try:
            text = self._screen_of(s.id, s.terminal)
        except Exception:
            text = None
        if not text:
            return
        seen = read_screen(text)
        self._screen_read[s.id] = self.clock()
        if seen["mode"] and seen["mode"] != s.mode:
            s.mode = seen["mode"]
            self._modes.add(seen["mode"])
            self.hub.version += 1
        if seen["running"] is not None and seen["running"] != self._background.get(s.id):
            self._background[s.id] = seen["running"]
            self.hub.version += 1
        if self.cfg.log_level == "debug":
            brief = {k: v for k, v in seen.items() if k != "btw"} | {"btw": bool(seen["btw"])}
            if self._seen_before.get(s.id) != brief:
                self._seen_before[s.id] = brief
                self.note(f"screen of {self.name_of(s)}: {brief}", debug=True)
        if seen["queued"] != (s.id in self._screen_queued):
            (self._screen_queued.add if seen["queued"] else self._screen_queued.discard)(s.id)
            self.hub.version += 1
        self._side_answer(s, seen["btw"])

    def _side_answer(self, s: Session, btw: dict | None) -> None:
        """Read out the answer to a /btw question. It is only on the screen (no hook fires for it) and arrives
        bit by bit, so it is spoken once it has been the same on two looks in a row.

        A view is announced once. If more text follows after that, only what was added is spoken. Text that
        merely changed its line breaks (the window was resized) or scrolled is not read again."""
        if not btw:
            self._btw_seen.pop(s.id, None)  # the view is closed
            return
        text = " ".join(btw["answer"].split())  # without line breaks: a reflow is not a change
        if not text:
            return
        seen = self._btw_seen.get(s.id)
        if seen is None or (btw["question"] and seen["question"] != btw["question"]):
            self._btw_seen[s.id] = {"question": btw["question"], "text": text, "same": 0, "spoken": ""}
            return
        if text != seen["text"]:
            seen["text"], seen["same"] = text, 0  # still growing: wait until it settles
            return
        seen["same"] += 1
        if seen["same"] < 2 or text == seen["spoken"]:
            return
        if self.take or self._sending or (self.speaker and self.speaker.speaking):
            return  # not while the microphone is open (it would record the voice) or something is being said: later
        added, first = text, not seen["spoken"]
        if seen["spoken"]:
            # What is new? Normally the text read so far, plus more. If the answer has scrolled, its beginning is
            # gone from the screen: then the end of what was read is found again at the top of what is visible
            # now, and everything after that is new. If nothing of it can be found, nothing is said: reading the
            # visible part again would repeat what was heard.
            done = _overlap(seen["spoken"], text)
            if done == 0:
                seen["spoken"] = text
                return
            added = text[done:].strip()
        seen["spoken"] = text
        if added and self.speaker and self.cfg.speech.speak_on_stop and not self._muted(s) and not self.muted:
            self.speaker.speak((f"{self.name_of(s)}, by the way: " if first else "") + speakable(added))

    def _side_view_open(self, s: Session) -> bool:
        """Is a /btw answer open in the session's terminal right now? Looks at the screen this moment, because
        Esc is only safe while that view is open: otherwise Esc would interrupt Claude."""
        try:
            text = self._screen_of(s.id, s.terminal)
        except Exception:
            return False
        return bool(text) and read_screen(text)["btw"] is not None

    def _has_queue(self, s: Session) -> bool:
        """Is a message waiting in the session's queue, i.e. is there something for "send now"?
        Where the terminal's screen can be read, that decides: Claude Code shows "... to send now" exactly while
        something waits. (It may take a queued message in the middle of a turn, or not have queued it at all.)
        Elsewhere it is a guess: the deck sent text while Claude was busy, the turn has not ended, and it is recent."""
        sent = self._queued.get(s.id)
        read = self._screen_read.get(s.id)
        if read is not None and (sent is None or read > sent[1] + 0.8):
            return s.id in self._screen_queued
        return sent is not None and sent[0] == s.turns and self.clock() - sent[1] < QUEUE_GUESS_S

    def _watch_loop(self) -> None:
        """The selected session about once a second (and at once when you select another one or press the mode
        key), the others every few seconds."""
        last: dict[str, float] = {}
        while not self._closed:
            self._watch_now.wait(0.5)
            self._watch_now.clear()
            if self._closed:
                return
            now, cur = time.monotonic(), self.hub.current()
            due = [s for s in self.hub.ordered()
                   if now - last.get(s.id, -1e9) >= (1.0 if cur is not None and s.id == cur.id else 6.0)]
            if cur is not None and self._watch_first == cur.id:
                due = [cur] + [s for s in due if s.id != cur.id]  # asked for: right now, whatever its turn
                self._watch_first = None
            for s in due[:2]:
                last[s.id] = now
                self._watch(s)

    def watch_now(self, session_id: str | None = None) -> None:
        self._watch_first = session_id
        self._watch_now.set()

    # -- stats survive a restart -----------------------------------------------
    def _load_stats(self) -> None:
        import json

        try:
            d = json.loads((self.cfg.home / "stats.json").read_text())
            self._stats_saved = {"limits": dict(d.get("limits") or {}), "sessions": dict(d.get("sessions") or {})}
            self.hub.limits.update(self._stats_saved["limits"])
        except (OSError, ValueError, AttributeError, TypeError):
            self._stats_saved = {"limits": {}, "sessions": {}}

    def _save_stats(self, now: float) -> None:
        """Context, cost and quota as last reported, so that they are there again right after a restart
        (Claude Code only reports them when a session does something)."""
        import json

        if now - self._stats_written < 10:
            return
        self._stats_written = now
        for s in self.hub.ordered():  # a session that has not reported since the start: what it reported last time
            if not s.heard and not any(v is not None for k, v in s.stats.items() if k not in LIMIT_KEYS):
                s.stats = {**self._stats_saved["sessions"].get(s.id, {}), **s.stats}
        sessions = {**self._stats_saved["sessions"], **{s.id: s.stats for s in self.hub.ordered() if s.stats}}
        if self.running:  # forget sessions that no longer run (only once it is known which ones do)
            keep = {r.id for r in self.running} | set(self.hub.sessions)
            sessions = {k: v for k, v in sessions.items() if k in keep}
        d = {"limits": dict(self.hub.limits), "sessions": sessions}
        if d == self._stats_saved:
            return
        self._stats_saved = d
        try:
            self.cfg.home.mkdir(parents=True, exist_ok=True)
            tmp = self.cfg.home / "stats.json.tmp"
            tmp.write_text(json.dumps(d))
            tmp.replace(self.cfg.home / "stats.json")
        except (OSError, TypeError, ValueError):
            pass

    # Detached sessions are remembered across restarts of Decktate (and of the computer): a session you
    # sent away stays away until you attach it again. Entries are dropped when you attach the session,
    # when you quit it from the deck, or after DETACHED_KEEP_DAYS.
    def _load_detached(self) -> None:
        import json

        try:
            data = json.loads((self.cfg.home / "detached.json").read_text())
            cutoff = time.time() - DETACHED_KEEP_DAYS * 86400
            self._detached_at = {str(k): float(v) for k, v in data.items() if float(v) >= cutoff}
        except (OSError, ValueError, AttributeError, TypeError):
            self._detached_at = {}
        self.hub.detached.update(self._detached_at)

    def _save_detached(self) -> None:
        """Write the list when it changed (also when the hub dropped a session that ended)."""
        import json

        now = {sid: self._detached_at.get(sid, time.time()) for sid in self.hub.detached}
        if now == self._detached_at:
            return
        self._detached_at = now
        try:
            self.cfg.home.mkdir(parents=True, exist_ok=True)
            tmp = self.cfg.home / "detached.json.tmp"
            tmp.write_text(json.dumps(now, indent=1))
            tmp.replace(self.cfg.home / "detached.json")
        except OSError:
            pass  # not remembered this time; nothing else depends on it

    # Which session is on which key, and which one you talk to, is remembered too (<home>/layout.json):
    # after a restart of Decktate the keys come back exactly as they were.
    def _load_layout(self) -> None:
        import json

        try:
            d = json.loads((self.cfg.home / "layout.json").read_text())
            last = [(x if isinstance(x, str) else None) for x in d["keys"]][:3]
            places = [str(x or "") for x in d["places"]][:3]
            if len(last) != 3 or len(places) != 3:
                return
            self._slot_last, self._slot_place = last, places
            now = self.clock()
            self._slot_freed = [now if x else -1e9 for x in last]  # hold each key for its session for a moment
            if isinstance(d.get("selected"), str):
                self.hub.selected = d["selected"]
                self._chosen = (d["selected"], str(d.get("selected_place") or ""), now)
            self._layout_saved = d
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def _save_layout(self) -> None:
        import json

        cid, where, _ = self._chosen
        d = {"keys": list(self._slot_last), "places": list(self._slot_place), "selected": cid, "selected_place": where}
        if d == self._layout_saved or (cid is None and self._layout_saved and not self.hub.sessions):
            return  # unchanged, or nothing is running (yet): keep what was
        self._layout_saved = d
        try:
            self.cfg.home.mkdir(parents=True, exist_ok=True)
            tmp = self.cfg.home / "layout.json.tmp"
            tmp.write_text(json.dumps(d, indent=1))
            tmp.replace(self.cfg.home / "layout.json")
        except OSError:
            pass

    def unattached(self) -> list:
        """Running sessions the deck does not look after: detached ones, and ones it has never heard from."""
        return [r for r in self.running if r.id not in self.hub.sessions]

    def _attach(self, r) -> None:
        self.hub.attach(r.id, cwd=r.cwd, title=r.title, pid=r.pid, terminal=self._terminal_env(r.pid), started=r.started or None)
        self._look(self.clock(), force=True)
        self._save_detached()
        if self.speaker:
            self.speaker.chime("ok")
        s = self.hub.sessions.get(r.id)
        self.toast(f"attached {self.name_of(s) if s else ''}".strip())

    def _detach(self, session_id: str) -> None:
        s = self.hub.sessions.get(session_id)
        name = self.name_of(s) if s else ""
        if self.hub.detach(session_id):
            self.toast(f"detached {name}".strip(), Status.WARN)
        self._save_detached()
        self._go(self.menu_from)

    def _quit_session(self, session_id: str) -> None:
        """Close the Claude Code session itself (hold to confirm). The conversation can be resumed later."""
        s = self.hub.sessions.get(session_id)
        pid = s.pid if s else next((r.pid for r in self.running if r.id == session_id), 0)
        name = self.name_of(s) if s else ""
        ok = bool(pid) and self._quit(pid)
        if self.hub.audit:
            self.hub.audit.write("quit", session_id, pid=pid, sent=ok)
        if not ok:
            return self.toast("cannot quit it", Status.FAIL, 4)
        self._quitting.add(pid)  # it takes a moment to exit: do not pick it up again meanwhile
        self._no_return.add(session_id)
        self.hub.forget(session_id)
        self.running = [r for r in self.running if r.id != session_id]
        if self.speaker:
            self.speaker.chime("no")
        self.toast(f"quit {name}".strip(), Status.WARN)
        self._go("main")

    def _open_menu(self, session_id: str) -> None:
        # remember where the menu was opened (home screen or the list): that is where it returns to
        self.menu_from = self.page if self.page in ("main", "sessions") else "main"
        self.menu_for, self.page, self.page_no = session_id, "session", 0

    def _session_menu(self) -> dict[int, Key]:
        """Opened by holding a session key.
        [ the session ] [        ] [        ] [      ] [      ]
        [ talk to     ] [ detach ] [ quit   ] [      ] [ back ]      (not attached: [ attach ] [ ] [ quit ])
        """
        sid = self.menu_for
        s = self.hub.sessions.get(sid)
        r = next((x for x in self.running if x.id == sid), None)
        if not s and not r:  # it ended meanwhile
            self._go("main")
            return self._main(self.hub.current())
        back = Key(Face(kind="icon", icon="back", label="back", accent=ACCENT), tap=lambda: self._go(self.menu_from))
        quit_key = Key(Face(kind="icon", icon="stop", status=Status.FAIL, label="quit", caption="hold 2 s"),
                       tap=lambda: self.toast("hold to quit", Status.WARN),  # a tap must not close a session
                       confirm=lambda: self._quit_session(sid), colour=COLORS[Status.FAIL])
        can_quit = bool((s and s.pid) or r)
        if s:
            cur = self.hub.current()
            head = self._session_key(s, cur is not None and cur.id == s.id)
            head.tap = head.hold = None

            def talk():
                self._select(s)
                self._go("main")

            keys = {0: head,
                    5: Key(Face(kind="icon", icon="mic", label="talk to", status=Status.OK, caption="select"), tap=talk),
                    6: Key(Face(kind="icon", icon="cross", label="detach", status=Status.WARN, caption="leave it"),
                           tap=lambda: self._detach(sid))}
        else:
            head = self._unattached_key(r)
            head.tap = head.hold = None
            keys = {0: head,
                    5: Key(Face(kind="icon", icon="check", label="attach", status=Status.OK),
                           tap=lambda: (self._attach(r), self._go(self.menu_from)))}
        if can_quit:
            keys[7] = quit_key
        keys[9] = back
        return keys

    def _unattached_key(self, r) -> Key:
        """A running session the deck does not look after: grey. Tap attaches it, hold opens its menu."""
        import os

        name = self._numbered(r.id, r.title or os.path.basename(r.cwd.rstrip("/")) or r.id[:6])
        face = Face(kind="session", text=name, value="detached", status=Status.UNKNOWN)
        return Key(face, tap=lambda: self._attach(r), hold=lambda: self._open_menu(r.id))

    # -- dictation -----------------------------------------------------------
    def _talk_start(self, purpose: str) -> None:
        if not self._new_take:
            return self.toast("no speech key", Status.FAIL)
        if self._sending:
            return self.toast("one moment", Status.WARN)
        if self.speaker and self.speaker.speaking:
            self.speaker.stop()  # or the microphone would record the voice
        cur = self.hub.current()
        take = self._new_take()
        if not take.start():
            return self.toast(take.error or "mic failed", Status.FAIL, 4)
        self.take, self.take_for, self.review, self._levels = take, purpose, None, []
        # whatever happens on the deck while you talk, the words go to the session that was selected when you started
        self.take_target, self._take_ready, self._latched = (cur.id if cur else None), False, None
        self.note(f"listening ({purpose}) for {self.name_of(cur) if cur else 'no session'}")

    def _talk_latch(self) -> None:
        """The key was tapped, not held: keep listening, hands free, until it is tapped again."""
        if self.take:
            self._latched = self.clock()
            self.note("tap: listening hands-free until the key is tapped again")

    def _talk_discard(self, why: str = "discarded") -> None:
        take, self.take, self._latched = self.take, None, None
        if take:
            take.cancel()
            if self.speaker:
                self.speaker.chime("no")
            self.toast(why, Status.WARN)

    def _talk_finish(self) -> None:
        take, self.take, self._latched = self.take, None, None
        if not take:
            return
        purpose, target = self.take_for, self.take_target
        if self.speaker:
            self.speaker.chime("stop")
        self._sending = purpose

        def run():
            try:
                text = take.finish()  # records the tail, waits for the final text: up to a few seconds
            except Exception as exc:
                text, take.error = "", type(exc).__name__
            finally:
                self._sending = ""
            self._heard(purpose, target, text, take.error)

        run() if self.sync else threading.Thread(target=run, daemon=True).start()

    def _heard(self, purpose: str, target: str | None, text: str, error: str) -> None:
        self.note(f"heard {len(text.split())} words ({purpose})" + (f", error: {error}" if error else ""))
        if error and not text:
            return self.toast(error, Status.FAIL, 4)
        if purpose == "reason":
            self._decide("deny", text)
        elif purpose == "test":
            # the microphone test shows what it heard on the deck; the log only gets how much that was
            self.toast(text or "heard nothing", Status.OK if text else Status.WARN, 4,
                       logged=f"what the microphone test heard, {len(text.split())} words" if text else None)
        elif not text:
            self.toast("heard nothing", Status.WARN)
        elif purpose in TALK_COMMANDS:  # e.g. "/btw what does this function return?"
            s = self.hub.sessions.get(target) if target else None
            if not s:
                return self.toast("session is gone", Status.FAIL, 4)
            self._type("text", f"{purpose} {text}", s.id)
            self.toast(f"asked {self.name_of(s)}")
            if not self.sync:  # the answer comes on the screen: start looking for it
                threading.Timer(1.5, self.watch_now, args=(s.id,)).start()
            self._go("main")
        elif self.cfg.speech.review_dictation:
            self.review = text  # shown on the top row; MIC sends, the cross discards
        else:
            s = self.hub.sessions.get(target) if target else None
            if not s:
                return self.toast("session is gone", Status.FAIL, 4)
            self._type("text", text, s.id)  # sent as soon as the key is released
            self.toast(f"sent to {self.name_of(s)}")

    def _send_review(self) -> None:
        text, self.review = self.review, None
        if text:
            self._type("text", text, self.take_target if self.take_target in self.hub.sessions else None)
            self.toast("sent")

    # -- menus ---------------------------------------------------------------
    def _menu_state(self, s: Session) -> dict:
        if self.menu.get("pending") is not s.pending:
            self.menu = {"pending": s.pending, "index": 0, "answers": {}, "picked": set()}
        return self.menu

    def _question_index(self, s: Session) -> int:
        return self._menu_state(s)["index"] if s.pending else 0

    def _menu_pick(self, s: Session, option: int) -> None:
        m = self._menu_state(s)
        q = s.pending.questions[m["index"]]
        if q.get("multiSelect"):
            m["picked"] ^= {option}
        else:
            m["picked"] = {option}
            self._menu_confirm(s)

    def _menu_confirm(self, s: Session) -> None:
        m = self._menu_state(s)
        questions = s.pending.questions
        q = questions[m["index"]]
        labels = [o.get("label", "") for i, o in enumerate(q.get("options") or []) if i in m["picked"]]
        if not labels:
            return self.toast("pick one", Status.WARN)
        m["answers"][q.get("question", "")] = ", ".join(labels)
        m["picked"] = set()
        if m["index"] + 1 < len(questions):
            m["index"] += 1
            self.say(self.describe_session(s))
        elif self.hub.answer(s.id, m["answers"]):
            if self.speaker:
                self.speaker.chime("ok")
            self.toast("answered")

    # -- key map -------------------------------------------------------------
    def _session_key(self, s: Session, selected: bool) -> Key:
        """The name, large, and underneath a pictogram and a word for the state. The session you are
        talking to has the coloured outline and background; while you dictate it says "listening"."""
        icon, word = STATE_FACE[s.state]
        status = s.state.status
        if s.state == State.WORKING and s.activity:
            word = s.activity
        hears = (self.take_for == "prompt" or self.take_for in TALK_COMMANDS) and self.take_target == s.id
        if hears and self.take:
            icon, word, status = "mic", "listening", Status.OK
        elif hears and self._sending:
            icon, word, status = "send", "sending", Status.OK
        face = Face(kind="session", text=self.name_of(s), value=word, icon=icon, status=status,
                    solid=s.state.needs_you and s.state != State.WAITING, blink=s.state.needs_you,
                    pulse=s.state == State.WORKING, outline=ACCENT if selected else None)
        return Key(face, tap=lambda: self._select(s), hold=lambda: self._open_menu(s.id))

    def queue(self) -> list[Session]:
        """Sessions by urgency (who gets a free key, what "more" counts): those waiting for a decision first,
        then the selected one, then the others that want attention, then the rest;
        within each group the most recently active first."""
        cur = self.hub.current()

        def rank(s: Session) -> int:
            if s.pending or s.state in (State.PERMISSION, State.QUESTION):
                return 0
            if cur is not None and s.id == cur.id:
                return 1
            return 2 if s.state.needs_you else 3

        return sorted(self.hub.ordered(), key=lambda s: (rank(s), -s.updated))

    def slots(self) -> list[Session | None]:
        """The sessions on the three session keys. A session keeps its key for as long as it exists:
        nothing moves because a session got busy, finished or was selected. Only two things change a key:
        when one becomes free the sessions to its right move up and the free key on the right is given to the
        session that is first in the queue; and if the selected session has
        no key (picked from the "more" page, or it asks for a decision) it takes the key of the session
        that is last in the queue."""
        alive, now = self.hub.sessions, self.clock()
        for i, sid in enumerate(self._slots):
            if sid in alive:
                self._slot_place[i], self._slot_last[i] = place(alive[sid]) or self._slot_place[i], sid
            elif sid is not None:
                self._slots[i], self._slot_freed[i] = None, now
            if self._slots[i] is None and (self._slot_last[i] in self.hub.detached or self._slot_last[i] in self._no_return):
                # detached or quit from the deck: it is not coming back, the key is free at once
                self._slot_last[i], self._slot_place[i], self._slot_freed[i] = None, "", -1e9
        # close the gaps: the sessions move left over a key that is free for good (one that still waits for its
        # session to come back keeps its place); free keys end up on the right
        keep = sorted(range(len(self._slots)), key=lambda i: self._slots[i] is None and now - self._slot_freed[i] >= RETURN_S)
        if keep != list(range(len(self._slots))):
            for name in ("_slots", "_slot_place", "_slot_last", "_slot_freed"):
                setattr(self, name, [getattr(self, name)[i] for i in keep])
        order = self.queue()
        waiting = [s for s in order if s.id not in self._slots]
        for i, sid in enumerate(self._slots):
            # first: the session that had this key gets it back - the same session (after a restart of Decktate),
            # or a new one in the same terminal (/clear, /resume)
            back = (next((s for s in waiting if s.id == self._slot_last[i]), None)
                    or next((s for s in waiting if self._slot_place[i] and place(s) == self._slot_place[i]), None))
            if sid is None and back:
                self._slots[i] = back.id
                waiting.remove(back)
        reserved = {p for i, p in enumerate(self._slot_place) if p and self._slots[i] is None}
        for i, sid in enumerate(self._slots):
            # a key that just became free waits a moment for its session to come back (/clear starts a new one)
            if sid is None and now - self._slot_freed[i] >= RETURN_S:
                nxt = next((s for s in waiting if place(s) not in reserved - {self._slot_place[i]}), None)
                if nxt:
                    self._slots[i] = nxt.id
                    waiting.remove(nxt)
        cur = self.hub.current()
        if cur and cur.id not in self._slots:
            rank = {s.id: n for n, s in enumerate(order)}
            last = max(range(len(self._slots)), key=lambda i: rank.get(self._slots[i], -1))
            self._slots[last] = cur.id
        if self._slots != self._slots_logged:
            self._slots_logged = list(self._slots)
            self.note("session keys: " + ", ".join(self.name_of(alive[sid]) if sid in alive else "-" for sid in self._slots)
                      + f" | talking to: {self.name_of(cur) if cur else '-'}", debug=True)
        return [alive.get(sid) if sid else None for sid in self._slots]

    def _stick(self, now: float) -> None:
        """Keep talking to the same terminal. The selection only changes when you press a key; if the selected
        session ends and a new one starts in the same terminal (/clear, /resume), that one is selected again."""
        hub = self.hub
        cid, where, since = self._chosen
        with hub._lock:
            if cid in hub.sessions and hub.selected not in hub.sessions:
                hub.selected = cid  # it is back (Decktate restarted, or it was away for a moment)
            if cid is None or cid in hub.sessions:
                sel = hub.selected if hub.selected in hub.sessions else None
                self._chosen = (sel, place(hub.sessions[sel]) if sel else "", now)  # follow: any change was a key press
                return
            heir = next((s for s in hub.ordered() if where and place(s) == where), None)
            if heir:
                hub.selected = heir.id
                self._chosen = (heir.id, where, now)
                return
            if hub.selected not in hub.sessions:  # meanwhile: a session that has a key, so that no key changes hands
                hub.selected = next((sid for sid in self._slots if sid in hub.sessions), None)
            if now - since > RETURN_S and hub.sessions:  # it is not coming back (while nothing runs at all: keep waiting)
                self._chosen = (None, "", now)

    def _go(self, page: str, topic: str | None = None) -> None:
        """Open a page. `topic`: a settings topic to open directly (back then leads straight home)."""
        self.page, self.page_no, self.topic, self._topic_direct = page, 0, topic, topic is not None

    def _mode_key(self, s: Session | None) -> Key | None:
        if not s:
            return None  # nothing to switch
        icon, word, colour = MODES.get(s.mode, ("dots", s.mode, ACCENT))
        return Key(Face(kind="icon", icon=icon, label="mode", caption=word, accent=colour), tap=lambda: self._next_mode(s))

    def _next_mode(self, s: Session) -> None:
        """Shift+Tab in the session's terminal. Claude Code only reports the mode with its next hook, which can
        be a long time for an idle session, so the key shows the expected next mode at once; the next hook
        corrects it if Claude Code went somewhere else."""
        self._modes.add(s.mode)
        cycle = [m for m in MODE_CYCLE if m in self._modes]
        self._type("shift_tab", session=s.id)
        s.mode = cycle[(cycle.index(s.mode) + 1) % len(cycle)] if s.mode in cycle else "default"
        self.hub.version += 1
        if not self.sync:  # then look what Claude Code really switched to
            threading.Timer(0.9, self.watch_now, args=(s.id,)).start()

    def background(self, s: Session) -> dict:
        """What the session has running besides its own turn: background shells and monitors (as its terminal
        screen counts them) and subagents (from the hooks, or the screen's count of background agents if higher).
        Only what is not zero: {"shells": 2, "agents": 1}."""
        seen = self._background.get(s.id) or {}
        counts = {"shells": seen.get("shells", 0), "monitors": seen.get("monitors", 0),
                  "agents": max(s.subagents, seen.get("agents", 0)), "tasks": seen.get("tasks", 0)}
        return {k: v for k, v in counts.items() if v}

    def _running_key(self, s: Session | None) -> Key | None:
        """One key for everything that runs in the background of the selected session; dark when nothing does.
        Tap: hear it."""
        if not s:
            return None
        counts = self.background(s)
        if not counts:
            return None
        said = " and ".join(f"{n} {name if n != 1 else name[:-1]}" for name, n in counts.items())
        return Key(Face(kind="counts", stats=counts, status=Status.RUNNING),
                   tap=lambda: self.say(f"{self.name_of(s)} has {said} running."))

    def _stats_key(self, s: Session | None) -> Key | None:
        """Quota, context and cost at a glance; tap for the full stats page."""
        if not s:
            return None
        st = s.stats
        summary = {"h5": st.get("five_hour_pct"), "d7": st.get("seven_day_pct"), "ctx": st.get("context_pct"),
                   "cost": st.get("cost_usd")}
        face = (Face(kind="stats", stats=summary) if any(v is not None for v in summary.values())
                else Face(kind="icon", icon="chart", label="stats", accent=ACCENT))
        return Key(face, tap=lambda: self._go("stats"))

    def _back_key(self) -> Key:
        return Key(Face(kind="icon", icon="back", label="back", accent=ACCENT), tap=lambda: self._go("main"))

    def _main(self, cur: Session | None) -> dict[int, Key]:
        """
        [ S1  ] [ S2      ] [ S3            ] [ +N more  ] [ SETTINGS ]
        [ NO/STOP ] [ OK / SEND NOW ] [ ALWAYS  ] [ MODE   ] [ STATS    ]
        [ MIC     ] [ SPEAK         ] [ discard ] [        ] [  /       ]
        A key that has nothing to do right now is not shown.
        """
        keys: dict[int, Key | None] = {}
        shown = self.slots()
        for i, s in enumerate(shown):
            if s:
                keys[i] = self._session_key(s, cur is not None and s.id == cur.id)
        on_keys = {s.id for s in shown if s}
        hidden = [s for s in self.queue() if s.id not in on_keys]
        loose = len(self.unattached())
        if hidden or loose:  # the sessions that are not on the three keys: attached ones without a key, and detached ones
            needy = any(s.state.needs_you for s in hidden)
            keys[3] = Key(Face(value=f"+{len(hidden) + loose}", label="more", accent=ACCENT,
                               caption=f"{loose} detached" if loose else "sessions",
                               status=Status.WARN if needy else None, blink=needy), tap=lambda: self._go("sessions"))
        p = cur.pending if cur and cur.pending and cur.pending.kind == "permission" else None
        stt = self._new_take is not None
        if p:
            keys[6] = Key(Face(kind="icon", icon="check", status=Status.OK, label="approve", solid=True),
                          tap=lambda: self._decide("allow"), colour=COLORS[Status.OK])
            keys[5] = Key(Face(kind="icon", icon="cross", status=Status.FAIL, label="reject", solid=True,
                               caption="hold: why" if stt else ""),
                          tap=lambda: self._decide("deny"),
                          talk=(lambda: self._talk_start("reason"), self._talk_finish) if stt else None)
            if p.always_label:
                keys[7] = Key(Face(kind="text", text=p.always_label, label="always", status=Status.WARN),
                              confirm=lambda: self._decide("always"), colour=COLORS[Status.WARN])
        elif cur and cur.state not in (State.IDLE, State.DONE, State.WAITING, State.ERROR):
            # nothing to approve: the reject key's place is free for "stop", shown only while the session is busy
            keys[5] = Key(Face(kind="icon", icon="stop", status=Status.FAIL, label="interrupt"), tap=lambda: self._type("escape"))
            # Ctrl+Enter: text the deck sent while Claude is busy waits in its queue; this sends it now. Shown from
            # then until the turn ends (Claude Code does not tell when it takes a queued message, and it reports a
            # queued prompt as submitted right away, so that cannot be used). Text typed on the keyboard is not seen here.
            if self._has_queue(cur):
                keys[6] = Key(Face(kind="icon", icon="send", status=Status.OK, label="send now", caption="queued"),
                              tap=lambda: (self._type("ctrl_enter"), self.toast("send now")))
        keys[4] = Key(Face(kind="icon", icon="gear", label="settings", accent=ACCENT), tap=lambda: self._go("settings"))
        keys[8] = self._mode_key(cur)  # permission mode of the selected session; tap = Shift+Tab
        keys[RUNNING_KEY] = self._running_key(cur)
        keys[9] = self._stats_key(cur)
        if self.review is not None:
            keys[10] = Key(Face(kind="icon", icon="send", status=Status.OK, solid=True, label="send"), tap=self._send_review)
            keys[DISCARD_KEY] = Key(Face(kind="icon", icon="cross", status=Status.FAIL, label="discard"),
                                    tap=lambda: (setattr(self, "review", None), self.toast("discarded", Status.WARN)))
        elif stt and self._sending == "prompt":
            keys[10] = Key(Face(kind="icon", icon="send", label="sending", status=Status.OK))
        elif stt and cur:  # says who gets the words; green when it is your turn, i.e. Claude is not in the middle of something
            yours = cur.state not in BUSY
            keys[10] = Key(Face(kind="icon", icon="mic", label="talk to", caption=self.name_of(cur),
                                status=Status.OK if yours else None, accent=None if yours else ACCENT),
                           talk=(lambda: self._talk_start("prompt"), self._talk_finish))
        if self.speaker and self.speaker.enabled and self.muted:
            # the computer's volume is at zero: a crossed-out speaker; tap for the settings page with the volume keys
            keys[11] = Key(Face(kind="icon", icon="mute", label="muted", accent=MUTED, caption="volume 0"),
                           tap=lambda: self._go("settings", "sound"))
        elif self.speaker and self.speaker.enabled and self.speaker.speaking:
            # while it talks this key is the one to quieten it; the other keys leave it talking
            keys[11] = Key(Face(kind="icon", icon="mute", label="quiet", status=Status.WARN, solid=True, caption="tap"),
                           tap=lambda: self.speaker.stop())
        elif self.speaker and self.speaker.enabled and self.speaker.last_text:
            keys[11] = Key(Face(kind="icon", icon="speaker", label="speak", accent=ACCENT, caption="repeat"),
                           tap=lambda: self.speaker.repeat())
        if cur and self.cfg.slash:
            keys[14] = Key(Face(kind="icon", icon="slash", label="commands", accent=ACCENT), tap=lambda: self._go("commands"))
        if cur and cur.id in self._queued and self._queued[cur.id][0] != cur.turns:
            del self._queued[cur.id]  # the turn ended: Claude takes what was queued
        if self.take or self.review is not None:  # transcript across the top row
            keys.update(self._transcript_keys())
        return {k: v for k, v in keys.items() if v is not None}

    def _paged(self, items: list[Key]) -> dict[int, Key]:
        """Twelve keys per page in the 4x3 area; the right column pages and goes back."""
        slots = [0, 1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13]
        pages = max(1, -(-len(items) // len(slots)))
        self.page_no = max(0, min(self.page_no, pages - 1))
        start = self.page_no * len(slots)
        keys = dict(zip(slots, items[start:start + len(slots)]))
        counter = f"{self.page_no + 1}/{pages}"
        if self.page_no > 0:
            keys[4] = Key(Face(kind="icon", icon="up", label="page up", caption=counter, accent=ACCENT),
                          tap=lambda: setattr(self, "page_no", self.page_no - 1))
        keys[9] = self._back_key()
        if self.page_no < pages - 1:
            keys[14] = Key(Face(kind="icon", icon="down", label="page down", caption=counter, accent=ACCENT),
                           tap=lambda: setattr(self, "page_no", self.page_no + 1))
        return keys

    def _sessions(self, cur: Session | None) -> dict[int, Key]:
        """The sessions that are not on the three session keys, in the order they were started (so they do not move):
        attached ones with their state (tap: select it and go back), and the ones running on this computer
        that are not attached, grey (tap: attach). Hold: its menu."""
        def pick(s: Session):
            self._select(s)
            self._go("main")

        items = []
        on_keys = {s.id for s in self.slots() if s}
        for s in self.hub.ordered():
            if s.id in on_keys:
                continue
            key = self._session_key(s, cur is not None and s.id == cur.id)
            key.tap = lambda s=s: pick(s)
            items.append((s.started, key))
        items += [(r.started, self._unattached_key(r)) for r in self.unattached()]
        if not items:  # everything is on the keys
            self._go("main")
            return self._main(cur)
        return self._paged([key for _, key in sorted(items, key=lambda it: it[0])])

    def _commands(self, cur: Session | None) -> dict[int, Key]:
        """The slash commands. Tap sends one to the selected session; destructive ones need the confirm hold."""
        def send(cmd: str):
            self._type("text", cmd)
            self._go("main")

        items = []
        stt = self._new_take is not None
        for cmd in self.cfg.slash:
            if cmd.strip() in TALK_COMMANDS:
                # a command that needs words, e.g. /btw: a side question Claude answers while it keeps working.
                # Hold the key and ask; on release "/btw <what you said>" is sent.
                if self.take and self.take_for == cmd.strip():
                    items.append(Key(self._listening_face()))
                elif self._sending == cmd.strip():
                    items.append(Key(Face(kind="icon", icon="send", label="sending", status=Status.OK)))
                else:
                    ok = bool(cur) and stt
                    items.append(Key(Face(kind="text", text=cmd.strip(), label="hold: ask" if ok else "no speech key",
                                          status=Status.OK if ok else None, disabled=not ok),
                                     tap=(lambda: self.toast("hold and ask", Status.WARN)) if ok else None,
                                     talk=(lambda c=cmd.strip(): self._talk_start(c), self._talk_finish) if ok else None))
                continue
            danger = cmd.strip().split()[0] in ("/clear", "/exit", "/quit")
            face = Face(kind="text", text=cmd, label="hold" if danger else "command", status=Status.WARN if danger else None)
            act = (lambda c=cmd: send(c)) if cur else None
            items.append(Key(face, confirm=act, colour=COLORS[Status.WARN]) if danger else Key(face, tap=act))
        return self._paged(items)

    def _listening_face(self) -> Face:
        """Level bars that follow your voice; if nothing arrives for a moment, it says so instead of looking busy."""
        heard = max(self._levels[-9:] or [0.0]) > 0.03
        ready = getattr(self.take, "ready", True)
        return Face(kind="bars", spark=[round(v, 2) for v in self._levels[-9:]], label="listening" if ready else "starting",
                    status=Status.OK if heard else Status.WARN,
                    caption="wait" if not ready else "hearing you" if heard else
                    ("no sound" if len(self._levels) >= 9 else "speak"))

    def _transcript_keys(self) -> dict[int, Key]:
        """While dictating: only the held key changes, to a level meter that says "listening".
        The text is not shown on the keys; it goes to Claude when the key is released.
        (With review_dictation on, the finished text is laid out on the top row to check.)"""
        if self.take:
            face = self._listening_face()
            if self._latched is not None:  # started with a tap: this key sends, the cross drops it
                face.label = "tap to send"
                return {10: Key(face, tap=self._talk_finish),
                        DISCARD_KEY: Key(Face(kind="icon", icon="cross", status=Status.FAIL, label="discard"),
                                         tap=self._talk_discard)}
            return {10 if self.take_for != "reason" else 5: Key(face)}
        words = (self.review or "").split()
        per = max(6, -(-len(words) // 4))
        return {i: Key(Face(kind="text", text=" ".join(words[i * per:(i + 1) * per]), label="dictated" if i == 0 else " "))
                for i in range(4)}

    def _menu(self, s: Session) -> dict[int, Key]:
        m = self._menu_state(s)
        questions = s.pending.questions
        q = questions[m["index"]]
        options = q.get("options") or []
        multi = bool(q.get("multiSelect"))
        slots = [1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13]
        count = f" {m['index'] + 1}/{len(questions)}" if len(questions) > 1 else ""
        keys = {0: Key(Face(kind="text", text=q.get("header") or q.get("question", ""), label="question" + count,
                            status=Status.WARN, solid=True), tap=lambda: self.say(self.describe_session(s)))}
        for i, opt in enumerate(options[: len(slots)]):
            picked = i in m["picked"]
            keys[slots[i]] = Key(Face(kind="text", text=opt.get("label", ""), label=f"option {i + 1}",
                                      status=Status.OK if picked else None, solid=picked),
                                 tap=lambda i=i: self._menu_pick(s, i))
        keys[4] = Key(Face(kind="icon", icon="check", status=Status.OK, label="confirm", solid=multi and bool(m["picked"]),
                           disabled=not (multi and m["picked"]), caption=f"{len(m['picked'])} picked" if multi else ""),
                      tap=(lambda: self._menu_confirm(s)) if multi and m["picked"] else None)
        keys[9] = Key(Face(kind="icon", icon="speaker", label="read out", accent=ACCENT),
                      tap=lambda: self.say(self.describe_session(s)))
        keys[14] = Key(Face(kind="icon", icon="back", label="in terminal", status=Status.FAIL),
                       tap=lambda: (self.hub.hand_back(s.id, "cancelled on the deck"), self.toast("in terminal", Status.WARN)))
        return keys

    def _stats(self, cur: Session | None) -> dict[int, Key]:
        keys: dict[int, Key] = {9: self._back_key()}
        if not cur:
            return keys
        st = cur.stats

        def ring(label, pct):
            if pct is None:
                return Key(Face(kind="ring", ring=0, value="–", label=label, status=Status.IDLE))
            status = Status.FAIL if pct >= 90 else Status.WARN if pct >= 70 else Status.OK
            return Key(Face(kind="ring", ring=pct / 100, value=f"{pct:.0f}", label=label, status=status))

        added, removed = st.get("lines_added"), st.get("lines_removed")
        keys.update({
            0: Key(Face(kind="text", text=self.name_of(cur), label="session", outline=ACCENT)),
            1: ring("context %", st.get("context_pct")),
            2: ring("5 hours %", st.get("five_hour_pct")),
            3: ring("7 days %", st.get("seven_day_pct")),
            5: Key(Face(kind="text", text=cur.model or "–", label="model")),
            6: Key(Face(value=money(st.get("cost_usd")), label="cost")),
            7: Key(Face(value=duration(st.get("duration_ms")), label="duration")),
            8: Key(Face(kind="text", text=f"+{added or 0}  −{removed or 0}" if added is not None else "–", label="lines")),
            # (the mode is already on the top-right key of every page)
            10: Key(Face(value=until(st.get("five_hour_reset")), label="5h resets in")),
            11: Key(Face(value=str(cur.subagents), label="subagents", status=Status.RUNNING if cur.subagents else None)),
            12: Key(Face(kind="text", text=st.get("effort") or "–", label="effort")),
            13: Key(Face(kind="text", text=st.get("version") or "–", label="claude code")),
        })
        return keys

    def _settings(self) -> dict[int, Key]:
        """The settings start with one key per topic (sound, microphone, voice, display) that shows what is set;
        a topic's own page has its few controls. Back leads from a topic to the topics, and from there home."""
        from ..speech import audio

        cfg = self.cfg
        mic, spk = audio.pick(cfg.audio.input, "input"), audio.pick(cfg.audio.output, "output")

        def cycle(kind: str):
            pool = audio.microphones() if kind == "input" else audio.speakers()
            if not pool:
                return self.toast("none found", Status.FAIL)
            cur = audio.pick(getattr(cfg.audio, kind), kind)
            nxt = pool[(pool.index(cur) + 1) % len(pool)] if cur in pool else pool[0]
            setattr(cfg.audio, kind, nxt.name)
            if kind == "output" and self.speaker:
                self.speaker.player.device = nxt.name
                self.speaker.chime("ok")
            cfg.save()

        def language():
            order = ["en", "de", "multi"]
            cfg.speech.language = order[(order.index(cfg.speech.language) + 1) % 3] if cfg.speech.language in order else "en"
            cfg.save()

        def volume(step: int):
            """Change the computer's own output volume, like the volume keys on the keyboard."""
            now = self._sysvol.get()
            if now is None:
                return self.toast("no volume control", Status.FAIL)
            new = max(0, min(100, round((now + step) / 10) * 10))
            if new == now:
                return self.toast("loudest" if step > 0 else "muted", Status.WARN)
            if not self._sysvol.set(new):
                return self.toast("no volume control", Status.FAIL)
            self._volume = (self.clock(), new)
            if self.speaker:
                self.speaker.chime("ok")   # hear the new level

        def speed_limits() -> tuple[float, float]:
            cloud = self.speaker and self.speaker.engine and self.speaker.engine.name == "elevenlabs"
            return (0.7, 1.2) if cloud else (0.7, 2.0)   # ElevenLabs tops out at 1.2

        def speed(step: float):
            lo, hi = speed_limits()
            new = round(min(hi, max(lo, cfg.speech.speed + step)), 2)
            if new == round(cfg.speech.speed, 2):
                return self.toast("fastest" if step > 0 else "slowest", Status.WARN)
            cfg.speech.speed = new
            cfg.save()
            self.say(f"Speed {new:g}. {SPEED_SAMPLE}")   # spoken at the new speed

        def voice():
            engine = self.speaker.engine if self.speaker else None
            if not engine or engine.name != "elevenlabs":
                return self.toast("system voice", Status.IDLE)
            try:
                self._voices = self._voices or engine.voices()
                ids = [v["id"] for v in self._voices]
                nxt = self._voices[(ids.index(engine.voice()) + 1) % len(ids)] if engine.voice() in ids else self._voices[0]
                engine._voice = cfg.speech.voice_id = nxt["id"]
                cfg.save()
                self.say(f"This is {nxt['name'].split(' - ')[0]}.")
            except Exception as exc:
                self.toast(type(exc).__name__, Status.FAIL)

        def bright(step: int):
            new = max(10, min(100, round((cfg.brightness + step) / 10) * 10))
            if new == cfg.brightness:
                return self.toast("brightest" if step > 0 else "darkest", Status.WARN)
            cfg.brightness = new
            self.deck.set_brightness(new)
            cfg.save()

        def toggle(name: str):
            setattr(cfg.speech, name, not getattr(cfg.speech, name))
            cfg.save()

        def switch(label: str, name: str) -> Key:
            on = getattr(cfg.speech, name)
            return Key(Face(kind="icon", icon="check" if on else "cross", label=label, caption="read out" if on else "silent",
                            status=Status.OK if on else None, accent=None if on else MUTED), tap=lambda: toggle(name))

        vol = self.system_volume()
        vol_text = "–" if vol is None else f"{vol}%"
        engine = self.speaker.engine.name if self.speaker and self.speaker.engine else "off"
        voice_name = next((v["name"].split(" - ")[0] for v in (self._voices or []) if v["id"] == cfg.speech.voice_id), engine)
        stt = self._new_take is not None
        hearing = max(self._levels[-9:] or [0.0]) > 0.03

        def log_level():
            cfg.log_level = "info" if cfg.log_level == "debug" else "debug"
            cfg.save()
            self.note(f"log level: {cfg.log_level}")
            self.toast(f"log: {'debug' if cfg.log_level == 'debug' else 'normal'}")

        def open_topic(name: str):
            self.topic, self._topic_direct = name, False

        def back():
            if self.topic is None or self._topic_direct:
                self._go("main")
            else:
                self.topic = None

        keys: dict[int, Key] = {9: Key(Face(kind="icon", icon="back", label="back", accent=ACCENT), tap=back)}
        if self.topic == "sound":       # [ speaker ] [ quieter ] [ louder ] [ test ]
            keys.update({
                0: Key(Face(kind="text", text=spk.name if spk else "none", label="speaker", status=None if spk else Status.FAIL),
                       tap=lambda: cycle("output")),
                1: Key(Face(kind="icon", icon="down", label="quieter", accent=ACCENT, caption=vol_text, disabled=vol in (None, 0)),
                       tap=lambda: volume(-10)),
                2: Key(Face(kind="icon", icon="up", label="louder", accent=ACCENT, caption=vol_text, disabled=vol in (None, 100)),
                       tap=lambda: volume(+10)),
                3: Key(Face(kind="icon", icon="speaker", label="test", accent=ACCENT, disabled=engine == "off"),
                       tap=lambda: self.say("Decktate is ready.")),
            })
        elif self.topic == "microphone":  # [ microphone ] [ language ] [ test ]
            keys.update({
                0: Key(Face(kind="text", text=mic.name if mic else "none", label="microphone", status=None if mic else Status.FAIL),
                       tap=lambda: cycle("input")),
                1: Key(Face(value=cfg.speech.language.upper(), label="language"), tap=language),
                2: Key(Face(kind="bars", spark=[round(v, 2) for v in self._levels[-9:]], label="test",
                            status=Status.OK if hearing else Status.WARN, caption="hearing you" if hearing else "no sound")
                       if self.take and self.take_for == "test" else
                       Face(kind="icon", icon="mic", label="test", caption="hold" if stt else "no key", accent=ACCENT, disabled=not stt),
                       talk=(lambda: self._talk_start("test"), self._talk_finish) if stt else None),
            })
        elif self.topic == "voice":     # [ voice ] [ slower ] [ speed ] [ faster ] / [ requests ] [ answers ]
            keys.update({
                0: Key(Face(kind="text", text=voice_name, label="voice"), tap=voice),
                1: Key(Face(kind="icon", icon="down", label="slower", accent=ACCENT, disabled=cfg.speech.speed <= speed_limits()[0]),
                       tap=lambda: speed(-SPEED_STEP)),
                2: Key(Face(value=f"{cfg.speech.speed:g}x", label="talk speed"), tap=lambda: self.say(SPEED_SAMPLE)),
                3: Key(Face(kind="icon", icon="up", label="faster", accent=ACCENT, disabled=cfg.speech.speed >= speed_limits()[1]),
                       tap=lambda: speed(+SPEED_STEP)),
                5: switch("requests", "speak_permissions"),   # permission requests and questions: read out or not
                6: switch("answers", "speak_on_stop"),        # what a session finished with: read out or not
            })
        elif self.topic == "display":   # [ dimmer ] [ brightness ] [ brighter ]
            keys.update({
                0: Key(Face(kind="icon", icon="down", label="dimmer", accent=ACCENT, disabled=cfg.brightness <= 10), tap=lambda: bright(-10)),
                1: Key(Face(value=str(cfg.brightness), label="brightness")),
                2: Key(Face(kind="icon", icon="up", label="brighter", accent=ACCENT, disabled=cfg.brightness >= 100), tap=lambda: bright(+10)),
            })
        else:                           # the topics: each key says what is set, and opens its page
            from ..claude import install

            try:
                hooks = install.installed(cfg)
            except Exception:
                hooks = {"complete": False, "events": []}
            keys.update({
                0: Key(Face(kind="icon", icon="mute" if vol == 0 else "speaker", label="sound", caption=vol_text, accent=ACCENT),
                       tap=lambda: open_topic("sound")),
                1: Key(Face(kind="icon", icon="mic", label="microphone", caption=mic.name if mic else "none",
                            accent=None if not mic else ACCENT, status=None if mic else Status.FAIL),
                       tap=lambda: open_topic("microphone")),
                2: Key(Face(kind="icon", icon="voice", label="voice", caption=f"{voice_name} {cfg.speech.speed:g}x", accent=ACCENT),
                       tap=lambda: open_topic("voice")),
                3: Key(Face(kind="icon", icon="sun", label="display", caption=f"{cfg.brightness}%", accent=ACCENT),
                       tap=lambda: open_topic("display")),
                5: Key(Face(kind="icon", icon="pass" if hooks["complete"] else "warn", label="hooks",
                            status=Status.OK if hooks["complete"] else Status.WARN,
                            caption="installed" if hooks["complete"] else "not set")),
                # how much is written to activity.log: tap to switch. "debug" records every key, hook and screen reading.
                6: Key(Face(value="debug" if cfg.log_level == "debug" else "normal", label="log",
                            status=Status.WARN if cfg.log_level == "debug" else None), tap=log_level),
            })
        return keys

    def keys(self) -> dict[int, Key]:
        cur = self.hub.current()
        if cur and cur.pending and cur.pending.kind == "question" and cur.pending.questions:
            keys = self._menu(cur)
        elif self.page == "stats":
            keys = self._stats(cur)
        elif self.page == "settings":
            keys = self._settings()
        elif self.page == "sessions":
            keys = self._sessions(cur)
        elif self.page == "session":
            keys = self._session_menu()
        elif self.page == "commands":
            keys = self._commands(cur)
        else:
            keys = self._main(cur)
        if self._toast and self.clock() < self._toast[2]:
            text, status, _ = self._toast
            slot = TOAST_KEY if TOAST_KEY not in keys else next((k for k in (14, 13, 12) if k not in keys), TOAST_KEY)
            keys[slot] = Key(Face(kind="text", text=text, label="result", status=status, solid=True),
                             tap=keys[slot].tap if slot in keys else None)
        return keys

    # -- input ---------------------------------------------------------------
    @staticmethod
    def _key_name(face: Face | None) -> str:
        """A key in words, for the log: its label, or what it shows."""
        if face is None:
            return "(empty)"
        return face.label or face.text or face.value or face.icon or face.kind

    def _process_input(self, now: float, keys: dict[int, Key]) -> None:
        while True:
            try:
                index, pressed = self._events.get_nowait()
            except queue.Empty:
                break
            if pressed:
                # speech is not cut off by using the deck: only the "quiet" key (and starting to dictate) stops it
                self._down = {"index": index, "key": keys.get(index) or Key(), "t0": now, "fired": False,
                              "talking": False, "silenced": False}
                face = self._down["key"].face
                self.note(f"key {index} down on page {self.page}: {self._key_name(face)}", debug=True)
            elif self._down and self._down["index"] == index:
                d, self._down = self._down, None
                k: Key = d["key"]
                self.note(f"key {index} up after {(now - d['t0']) * 1000:.0f} ms"
                          + (", was talking" if d["talking"] else "") + (", hold already fired" if d["fired"] else ""), debug=True)
                if d["talking"] and not k.tap and (now - d["t0"]) * 1000 < TAP_MS:
                    self._talk_latch()  # a tap on a talk-only key: listen until it is tapped again
                elif d["talking"]:
                    k.talk[1]()
                elif not d["fired"] and not d["silenced"] and k.tap and not (k.confirm and (now - d["t0"]) * 1000 >= self.cfg.hold_ms):
                    k.tap()
        d = self._down
        if d and not d["fired"]:
            k, held = d["key"], (now - d["t0"]) * 1000
            # a key that only talks starts recording the moment it goes down, so the first word is not lost;
            # a key that also has a tap (reject) waits until it is clear that this is a hold
            if k.talk and not d["talking"] and held >= (PTT_MS if k.tap else 0):
                d["talking"] = True
                k.talk[0]()
            elif k.confirm and held >= self.cfg.hold_ms + self.cfg.confirm_ms:
                d["fired"] = True
                k.confirm()
            elif k.hold and not k.confirm and held >= self.cfg.hold_ms:
                d["fired"] = True
                k.hold()

    def confirm_progress(self, now: float) -> tuple[int, float] | None:
        d = self._down
        if d and d["key"].confirm and not d["fired"]:
            p = ((now - d["t0"]) * 1000 - self.cfg.hold_ms) / self.cfg.confirm_ms
            if p > 0:
                return d["index"], min(1.0, p)
        return None

    # -- render --------------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self._look(now)
        self._stick(now)
        self._save_stats(now)
        self._save_detached()
        if self.take:
            self._levels = (self._levels + [float(self.take.level)])[-40:]
            if self._latched is not None and now - self._latched > LATCH_MAX_S:
                self._talk_discard("dictation timed out")  # forgotten: do not keep streaming the room
        if self.take:
            if not self._take_ready and getattr(self.take, "ready", True):
                self._take_ready = True
                if self.speaker:
                    self.speaker.chime("start")  # the microphone delivers sound: go
        keys = self.keys()
        self._process_input(now, keys)
        keys = self.keys()
        size = self.deck.key_size
        prog = self.confirm_progress(now)
        for index in range(self.deck.key_count):
            key = keys.get(index)
            img = render_key(key.face if key else None, size, now)
            if prog and prog[0] == index:
                img = snake(img, prog[1], key.colour if key else TEXT)
            raw = img.tobytes()
            if raw != self._sent[index]:
                self._sent[index] = raw
                self.deck.set_key_image(index, img)
        self._save_layout()

    def run(self, fps: float = 14) -> None:
        try:
            while True:
                t = time.monotonic()
                self.tick()
                time.sleep(max(0.0, 1 / fps - (time.monotonic() - t)))
        except KeyboardInterrupt:
            pass

    def close(self) -> None:
        self._closed = True
        self._watch_now.set()
        if self.take:
            self.take.cancel()
        self.hub.set_attached(False)
