"""macOS menu bar app: Decktate as an item at the top right of the screen.

`decktate app` runs the board inside an NSApplication without a Dock icon:
a status item with a small key grid, a number next to it when sessions need
you, and a menu (sessions, hooks, settings folder, quit).
`decktate install-app` writes a small Decktate.app bundle that starts it, so
it can be opened like any app and at login. Permissions (microphone,
controlling the terminal) are then granted to "Decktate" itself rather than
to whichever terminal started it.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from . import __version__
from .config import Config

BUNDLE_ID = "com.granig.decktate"
MENUBAR_FROM_RIGHT = 260.0  # where the menu bar item goes the first time: points from the right edge of the screen


def icon_image(size: int = 1024):
    """The app icon: a dark rounded tile with a small key grid and a microphone."""
    from PIL import Image, ImageDraw

    from .ui import icons
    from .ui.render import ACCENT, COLORS, _gradient
    from .status import Status

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile = _gradient(size, size).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    m = size * 0.085
    ImageDraw.Draw(mask).rounded_rectangle([m, m, size - m, size - m], radius=size * 0.2, fill=255)
    img.paste(tile, (0, 0), mask)
    d = ImageDraw.Draw(img)
    cols = [COLORS[Status.OK], COLORS[Status.RUNNING], COLORS[Status.WARN], ACCENT, COLORS[Status.FAIL], (70, 76, 96)]
    k, gap = size * 0.115, size * 0.035
    x0, y0 = size * 0.5 - (3 * k + 2 * gap) / 2, size * 0.63
    for i, c in enumerate(cols):
        x, y = x0 + (i % 3) * (k + gap), y0 + (i // 3) * (k + gap)
        d.rounded_rectangle([x, y, x + k, y + k], radius=k * 0.24, fill=c)
    icons.draw(d, "mic", size * 0.5, size * 0.37, size * 0.36, (245, 247, 250), size * 0.028)
    return img


def write_icns(path: Path) -> bool:
    """Build an .icns from the generated icon (needs macOS `iconutil`)."""
    if not shutil.which("iconutil"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Decktate.iconset"
        iconset.mkdir()
        base = icon_image(1024)
        for px in (16, 32, 128, 256, 512):
            base.resize((px, px)).save(iconset / f"icon_{px}x{px}.png")
            base.resize((px * 2, px * 2)).save(iconset / f"icon_{px}x{px}@2x.png")
        r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(path)], capture_output=True)
        return r.returncode == 0


LAUNCHER_C = r"""
// Decktate.app launcher: runs `python -m decktate app` inside THIS process, so
// macOS sees the application as Decktate (name, permissions) and not as Python.
#include <Python.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(void) {
    char path[4096];
    const char *home = getenv("HOME");
    if (home) {  // an app has no terminal: keep a log
        snprintf(path, sizeof path, "%s/.config/decktate", home);
        mkdir(path, 0700);
        snprintf(path, sizeof path, "%s/.config/decktate/decktate.log", home);
        int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd >= 0) { dup2(fd, 1); dup2(fd, 2); close(fd); }
    }
    PyStatus status;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    wchar_t *argv[] = {L"decktate", L"-m", L"decktate", L"app"};
    // pointing `executable` at the virtualenv's python makes its packages importable
    status = PyConfig_SetBytesString(&config, &config.executable, "@PYTHON@");
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesString(&config, &config.program_name, "@PYTHON@");
    if (!PyStatus_Exception(status)) status = PyConfig_SetArgv(&config, 4, argv);
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
    return Py_RunMain();
}
"""


def build_launcher(target: Path, python: str) -> str:
    """Compile the native launcher. Returns '' on success, else why it could not be built."""
    import sysconfig

    if not shutil.which("clang"):
        return "clang not found (install the Xcode command line tools: xcode-select --install)"
    v = sysconfig.get_config_vars()
    flags = [f"-I{v.get('INCLUDEPY')}"]
    if v.get("PYTHONFRAMEWORK"):
        prefix = v.get("PYTHONFRAMEWORKPREFIX")
        # link the framework's library by path: `-framework Python` is not found for Homebrew's layout
        flags += [str(Path(prefix) / v.get("LDLIBRARY", "Python.framework/Python")), f"-Wl,-rpath,{prefix}"]
    else:
        flags += [f"-L{v.get('LIBDIR')}", f"-lpython{v.get('LDVERSION')}", f"-Wl,-rpath,{v.get('LIBDIR')}"]
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "launcher.c"
        src.write_text(LAUNCHER_C.replace("@PYTHON@", python.replace("\\", "\\\\").replace('"', '\\"')))
        r = subprocess.run(["clang", "-O1", "-o", str(target), str(src), *flags], capture_output=True, text=True)
    return "" if r.returncode == 0 else (r.stderr.strip().splitlines() or ["clang failed"])[-1][:200]


def menubar_image(size: int = 36):
    """The menu bar glyph: a 3 x 2 key grid in one colour on transparent (a "template" image)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    S = size * 4
    k, gap = S * 0.235, S * 0.075
    x0, y0 = (S - (3 * k + 2 * gap)) / 2, (S - (2 * k + gap)) / 2
    for i in range(6):
        x, y = x0 + (i % 3) * (k + gap), y0 + (i // 3) * (k + gap)
        if i == 0:  # the first key is "lit"
            d.rounded_rectangle([x, y, x + k, y + k], radius=k * 0.28, fill=(0, 0, 0, 255))
        else:
            d.rounded_rectangle([x, y, x + k, y + k], radius=k * 0.28, outline=(0, 0, 0, 255), width=int(S * 0.035))
    return img.resize((size, size), Image.LANCZOS)


def install_app(directory: Path | None = None, python: str | None = None) -> tuple[Path, str]:
    """Write Decktate.app. It starts this installation (this Python, this code).
    Returns (path, note); note says so if only the fallback launcher could be made."""
    directory = Path(directory or "~/Applications").expanduser()
    app = directory / "Decktate.app"
    if app.exists():
        shutil.rmtree(app)
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir()
    python = python or sys.executable
    launcher = app / "Contents" / "MacOS" / "Decktate"
    note = build_launcher(launcher, python)
    if note:  # fallback: works, but macOS knows the process as "Python"
        launcher.write_text(f"""#!/bin/sh
mkdir -p "$HOME/.config/decktate"
exec "{python}" -m decktate app >> "$HOME/.config/decktate/decktate.log" 2>&1
""")
        note = f"native launcher not built ({note}); macOS will list the app as Python"
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    has_icon = write_icns(app / "Contents" / "Resources" / "Decktate.icns")
    info = {
        "CFBundleName": "Decktate", "CFBundleDisplayName": "Decktate", "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": "Decktate", "CFBundlePackageType": "APPL", "CFBundleVersion": __version__,
        "CFBundleShortVersionString": __version__, "LSMinimumSystemVersion": "12.0", "NSHighResolutionCapable": True,
        "LSUIElement": True,  # a menu bar app: no Dock icon
        "NSMicrophoneUsageDescription": "Decktate listens while you hold the dictate key, to turn speech into a prompt for Claude Code.",
        "NSAppleEventsUsageDescription": "Decktate types prompts and commands into the terminal that runs Claude Code.",
    }
    if has_icon:
        info["CFBundleIconFile"] = "Decktate"
    with (app / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump(info, f)
    # an ad-hoc signature gives the app a stable identity, so permission grants stick
    subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(app)], capture_output=True)
    return app, note


def run_app(cfg: Config, sim: bool = False, sim_port: int = 8766) -> int:
    """Run the board as a menu bar app (an item at the top right, no Dock icon). Blocks until Quit."""
    import AppKit
    import objc
    from Foundation import NSObject, NSTimer

    from .cli import build, open_deck
    from .claude import install

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # menu bar only: no Dock icon, no app menu

    def fail(title: str, detail: str) -> int:
        """An app without a window must not die silently: say what is wrong, then quit."""
        print(f"{title}: {detail}", file=sys.stderr)
        alert = AppKit.NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(detail)
        alert.addButtonWithTitle_("Quit")
        app.activateIgnoringOtherApps_(True)
        alert.runModal()
        return 1

    args = type("Args", (), {"sim": sim, "sim_port": sim_port})()
    try:
        deck = open_deck(args, cfg)
        hub, server, board = build(cfg, deck)
    except OSError as exc:
        try:
            deck.close()
        except Exception:
            pass
        return fail("Decktate cannot start",
                    f"Port {cfg.port} on this computer is already in use ({exc.strerror or exc}).\n\n"
                    "Either Decktate is already running, or another program uses that port. "
                    f"To use a different one, set `port:` in {cfg.home / 'config.yaml'} and run `decktate install` again.")
    except Exception as exc:
        return fail("Decktate cannot start", f"{type(exc).__name__}: {exc}")
    server.start()
    hooks = install.AutoHooks(cfg)
    problem = hooks.start()
    if problem:
        print(f"hooks not installed: {problem}", file=sys.stderr)
    stop = threading.Event()

    def loop():
        import time

        while not stop.is_set():
            t = time.monotonic()
            problem = board.step()  # e.g. the deck was unplugged: nothing is held meanwhile
            if problem:
                print(f"board: {problem}", file=sys.stderr)
                time.sleep(1)
            time.sleep(max(0.0, 1 / 14 - (time.monotonic() - t)))

    threading.Thread(target=loop, daemon=True, name="board").start()

    class Delegate(NSObject):
        def applicationDidFinishLaunching_(self, _note):
            # the menu bar item: a small key grid; a number next to it when sessions wait for you
            # macOS puts a new item at the left end of the status items. On a full menu bar of a MacBook with a
            # notch that is behind the notch: the item exists but cannot be seen or dragged. So the first time,
            # ask for a place near the right end (macOS remembers the place per item; Cmd-drag moves it later).
            defaults = AppKit.NSUserDefaults.standardUserDefaults()
            place = "NSStatusItem Preferred Position decktate"
            if defaults.objectForKey_(place) is None:
                defaults.setFloat_forKey_(MENUBAR_FROM_RIGHT, place)
            self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
            self.item.setAutosaveName_("decktate")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                menubar_image(36).save(f.name)
            image = AppKit.NSImage.alloc().initWithContentsOfFile_(f.name)
            os.unlink(f.name)
            button = self.item.button()
            if image is not None:
                image.setSize_((18, 18))
                image.setTemplate_(True)  # drawn in the menu bar's own colour, light or dark
                button.setImage_(image)
                button.setImagePosition_(AppKit.NSImageLeft)
            else:
                button.setTitle_("Decktate")
            button.setToolTip_("Decktate")
            self.menu = AppKit.NSMenu.alloc().init()
            self.menu.setDelegate_(self)
            self.menu.setAutoenablesItems_(False)
            self.item.setMenu_(self.menu)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "refresh:", None, True)

        def refresh_(self, _timer):
            needy = sum(1 for s in hub.sessions.values() if s.state.needs_you)
            self.item.button().setTitle_(f" {needy}" if needy else "")

        def menuNeedsUpdate_(self, menu):
            """Built each time the menu is opened."""
            menu.removeAllItems()

            def item(title, action=None, enabled=True):
                it = menu.addItemWithTitle_action_keyEquivalent_(title, action, "")
                it.setEnabled_(bool(action) and enabled)
                if action:
                    it.setTarget_(self)
                return it

            state = install.installed(cfg)
            sessions = hub.ordered()
            cur = hub.current()
            item("Stream Deck simulator" if sim else "No Stream Deck: requests are asked in the terminal"
                 if not board._answers or not board._deck_ok else "Stream Deck connected")
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            item(f"{len(sessions)} session{'s' if len(sessions) != 1 else ''}" if sessions else "No Claude Code sessions")
            for s in sessions:  # click: talk to it (the same as tapping its key)
                it = item(f"{board.name_of(s)}: {s.state.value}" + (f" ({s.activity})" if s.activity else ""), "selectSession:")
                it.setRepresentedObject_(s.id)
                it.setState_(1 if cur is not None and cur.id == s.id else 0)
            loose = len(board.unattached())
            if loose:
                item(f"{loose} detached")
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            engine = board.speaker.engine.name if board.speaker and board.speaker.engine else "off"
            item(f"Voice: {engine}   Dictation: {'on' if os.environ.get('DEEPGRAM_API_KEY') else 'no key'}")
            if state["complete"]:
                item("Remove Hooks from Claude Code", "removeHooks:")
            else:
                item("Install Hooks into Claude Code", "installHooks:")
            item("Open Settings Folder", "openFolder:")
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            quit_item = menu.addItemWithTitle_action_keyEquivalent_("Quit Decktate", "terminate:", "q")
            quit_item.setTarget_(app)

        def selectSession_(self, sender):
            s = hub.sessions.get(sender.representedObject())
            if s:
                board._select(s)
                board._go("main")

        def installHooks_(self, _sender):
            install.install(cfg, backup="once")
            hooks.active = cfg.auto_hooks  # remove them again on quit, as if added at start

        def removeHooks_(self, _sender):
            install.uninstall(cfg)
            hooks.active = False

        def openFolder_(self, _sender):
            cfg.home.mkdir(parents=True, exist_ok=True)
            subprocess.run(["open", str(cfg.home)])

        def applicationShouldTerminate_(self, _sender):
            stop.set()
            hooks.stop()  # first: no new requests arrive
            board.close()  # hands every held request back to the terminal
            hub.shutdown()
            server.stop()
            try:
                board.deck.close()
            except Exception:
                pass
            return AppKit.NSTerminateNow

    delegate = Delegate.alloc().init()
    app.setDelegate_(delegate)
    objc.setVerbose(False)
    import signal

    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))
    signal.signal(signal.SIGTERM, lambda *_: app.terminate_(None))
    # let Python handle signals while the Cocoa run loop is idle
    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, True, lambda _t: None)
    app.run()
    return 0
