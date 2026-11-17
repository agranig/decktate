"""Command line: decktate run | app | install-app | install | uninstall | doctor | devices | say."""

from __future__ import annotations

import argparse
import os
import signal
import sys

from . import __version__, config as config_mod
from .config import Config


def build(cfg: Config, deck, sync: bool = False, speaker="auto", clock=None):
    """Wire hub, hook server and board together. Returns (hub, server, board)."""
    import time

    from .core.audit import Audit
    from .core.hub import Hub
    from .core.server import HookServer
    from .ui.board import Board

    hub = Hub(hold_s=cfg.permission_hold_s, audit=Audit(cfg.audit_log))
    server = HookServer(hub, cfg.token(), cfg.port)
    if speaker == "auto":
        from .speech.tts import Speaker

        speaker = Speaker(cfg)
    take = None
    if os.environ.get("DEEPGRAM_API_KEY"):
        from .speech.stt import Dictation

        take = lambda: Dictation(cfg)  # noqa: E731
    board = Board(deck, hub, cfg, speaker, take, clock=clock or time.monotonic, sync=sync)
    return hub, server, board


def open_deck(args, cfg: Config):
    from .backend import SimDeck

    if not args.sim:
        from .backend.hid import DeckNotFound, HidDeck

        try:
            deck = HidDeck(brightness=cfg.brightness)
            info = deck.info()
            print(f"deck: {info['type']} (serial {info['serial']}, firmware {info['firmware']})")
            return deck
        except DeckNotFound as exc:
            print(f"{exc}; using the simulator", file=sys.stderr)
    deck = SimDeck()
    print("simulator:", deck.serve(args.sim_port, "127.0.0.1"))
    return deck


def cmd_run(args) -> int:
    cfg = config_mod.load()
    from .claude import install

    try:
        hub, server, board = build(cfg, open_deck(args, cfg))
    except OSError as exc:
        print(f"cannot listen on 127.0.0.1:{cfg.port}: {exc}\n"
              f"Decktate is already running, or another program uses that port. To change it, set `port:` in "
              f"{cfg.home / 'config.yaml'} and run `decktate install` again.", file=sys.stderr)
        return 1
    server.start()
    hooks = install.AutoHooks(cfg)
    problem = hooks.start()
    state = install.installed(cfg)
    engine = board.speaker.engine.name if board.speaker and board.speaker.engine else "off"
    how = ("installed for this run, removed on exit" if hooks.active else "installed" if state["complete"] else
           "NOT installed" + (f" ({problem})" if problem else ": run `decktate install`"))
    print(f"hooks: http://127.0.0.1:{server.port}/hook  ({how})")
    print(f"speech out: {engine}   speech in: {'deepgram' if os.environ.get('DEEPGRAM_API_KEY') else 'off (no DEEPGRAM_API_KEY)'}")
    print("Ctrl-C to quit")
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))  # `kill` cleans up too
    try:
        board.run()
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # a second Ctrl-C must not interrupt the clean-up
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        hooks.stop()  # first: no new requests arrive
        board.close()  # releases every held request: the terminal takes over
        hub.shutdown()
        server.stop()
        board.deck.close()
    return 0


def cmd_app(args) -> int:
    from .macapp import run_app

    return run_app(config_mod.load(), args.sim, args.sim_port)


def cmd_install_app(args) -> int:
    from .macapp import install_app

    app, note = install_app(args.dir)
    print(f"created {app}")
    if note:
        print("note:", note)
    print("Open it: it appears in the menu bar at the top right (no Dock icon). To start it at login, add it")
    print("under System Settings > General > Login Items.")
    print("It runs this installation; if you move or delete this folder, run `decktate install-app` again.")
    return 0


def cmd_install(args) -> int:
    from .claude import install

    cfg = config_mod.load()
    r = install.install(cfg, statusline=not args.no_statusline)
    print(f"hooks for {r['events']} events added to {r['settings']}")
    if r["backup"]:
        print(f"previous settings saved as {r['backup']}")
    if r["statusline"]:
        print(f"status line: {r['statusline']}")
    print("Running Claude Code sessions pick this up without a restart.")
    return 0


def cmd_uninstall(args) -> int:
    from .claude import install

    r = install.uninstall(config_mod.load())
    print(f"removed {r['removed']} hooks from {r['settings']}; status line restored")
    return 0


def cmd_devices(args) -> int:
    from .speech import audio

    cfg = config_mod.load()
    found = audio.devices()
    if not found:
        print("no audio devices found")
        return 1
    mic, spk = audio.pick(cfg.audio.input, "input"), audio.pick(cfg.audio.output, "output")
    for d in found:
        kinds = "/".join(k for k, n in (("mic", d.inputs), ("speaker", d.outputs)) if n)
        marks = [m for m, on in (("system default", d.default_in or d.default_out), ("USED AS MIC", mic and d.index == mic.index),
                                 ("USED AS SPEAKER", spk and d.index == spk.index)) if on]
        print(f"  [{d.index}] {d.name:40} {kinds:12} {', '.join(marks)}")
    print("choose with `audio: {input: <name>, output: <name>}` in the config, or on the deck (hold STATS)")
    return 0


def cmd_say(args) -> int:
    import time

    from .speech.tts import Speaker

    cfg = config_mod.load()
    sp = Speaker(cfg)
    if not sp.enabled:
        print("no speech engine available")
        return 1
    print(f"engine: {sp.engine.name}")
    sp.speak(" ".join(args.text) or "Decktate is ready.")
    time.sleep(0.3)
    while sp.speaking:
        time.sleep(0.1)
    if sp.error:
        print("error:", sp.error)
    return 1 if sp.error else 0


def cmd_doctor(args) -> int:
    """Check everything Decktate depends on and say what is missing."""
    import shutil

    from .claude import install
    from .speech import audio

    cfg = config_mod.load()
    ok = True

    def line(good, what, detail=""):
        nonlocal ok
        ok &= bool(good) or good is None
        print(f"  {'ok  ' if good else '--  ' if good is None else 'FAIL'} {what}{': ' + detail if detail else ''}")

    print(f"Decktate {__version__}   config: {cfg.home}")
    try:
        from .backend.hid import HidDeck

        d = HidDeck(brightness=cfg.brightness)
        info = d.info()
        d._deck.close()
        line(True, "Stream Deck", f"{info['type']}, {info['rows']}x{info['cols']} keys, firmware {info['firmware']}")
    except Exception as exc:
        line(False, "Stream Deck", str(exc))
    line(shutil.which("claude"), "Claude Code", shutil.which("claude") or "`claude` not on PATH")
    import socket

    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", cfg.port))
        line(True, "hook port", f"{cfg.port} is free")
    except OSError:
        try:
            mine = __import__("httpx").get(f"http://127.0.0.1:{cfg.port}/health", timeout=2).json().get("ok") is True
        except Exception:
            mine = False
        line(mine, "hook port", f"{cfg.port}: Decktate is running" if mine else
             f"{cfg.port} is used by another program: set `port:` in {cfg.home / 'config.yaml'}")
    finally:
        probe.close()
    state = install.installed(cfg)
    stale = state["complete"] and not state["current"]
    if cfg.auto_hooks and not state["complete"]:
        line(None, "hooks", "not installed right now: Decktate adds them when it starts and removes them when it quits")
    else:
        line(state["complete"] and not stale, "hooks", "installed" if state["complete"] and not stale else
             "point at another port: run `decktate install` again" if stale else "run `decktate install`")
    line(state["statusline"] or None, "status line (stats page)", "installed" if state["statusline"] else "not installed")
    mic, spk = audio.pick(cfg.audio.input, "input"), audio.pick(cfg.audio.output, "output")
    line(mic, "microphone", mic.name if mic else "none found")
    line(spk, "speaker", spk.name if spk else "none found")
    dg, el = os.environ.get("DEEPGRAM_API_KEY"), os.environ.get("ELEVENLABS_API_KEY")
    if dg:
        from .speech.stt import check_key

        why = check_key(dg)
        line(not why, "Deepgram key (dictation)", why or "valid")
    else:
        line(None, "Deepgram key (dictation)", f"not set: add DEEPGRAM_API_KEY to {cfg.home / '.env'}")
    if el:
        from .speech.tts import ElevenLabs

        try:
            why = ElevenLabs(el, cfg).check()
        except Exception as exc:
            why = type(exc).__name__
        line(not why, "ElevenLabs key (voice)", why or "valid")
    else:
        from .speech.tts import SystemVoice

        line(None, "ElevenLabs key (voice)", "not set: using the system voice" if SystemVoice.available() else "not set, and no system voice")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="decktate", description="A Stream Deck control surface for Claude Code.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="run the board (default)")
    run.add_argument("--sim", action="store_true", help="use the simulator web page instead of a deck")
    run.add_argument("--sim-port", type=int, default=8766)
    run.set_defaults(fn=cmd_run)
    app = sub.add_parser("app", help="run as a menu bar app")
    app.add_argument("--sim", action="store_true")
    app.add_argument("--sim-port", type=int, default=8766)
    app.set_defaults(fn=cmd_app)
    iapp = sub.add_parser("install-app", help="create Decktate.app (menu bar app)")
    iapp.add_argument("--dir", help="where to put it (default ~/Applications)")
    iapp.set_defaults(fn=cmd_install_app)
    inst = sub.add_parser("install", help="add Decktate's hooks to Claude Code's settings")
    inst.add_argument("--no-statusline", action="store_true", help="leave the status line alone (no stats page)")
    inst.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove Decktate's hooks and restore the status line").set_defaults(fn=cmd_uninstall)
    sub.add_parser("doctor", help="check deck, hooks, audio devices and API keys").set_defaults(fn=cmd_doctor)
    sub.add_parser("devices", help="list microphones and speakers").set_defaults(fn=cmd_devices)
    say = sub.add_parser("say", help="speak a sentence (tests the voice)")
    say.add_argument("text", nargs="*")
    say.set_defaults(fn=cmd_say)
    args = ap.parse_args(argv)
    if not args.cmd:
        args = ap.parse_args((argv or sys.argv[1:]) + ["run"])
    config_mod.load_env()
    try:
        sys.exit(args.fn(args))
    except KeyboardInterrupt:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        sys.exit(130)
