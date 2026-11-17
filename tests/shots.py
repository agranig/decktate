"""Render every page of the board to PNG (simulator, fake data): uv run python tests/shots.py [outdir]"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_decktate import BASH, QUESTION, Rig  # noqa: E402


KEY = 144  # pixels per key in the pictures: twice the deck's own 72, drawn at that size (not scaled up)


def picture(board, key: int = KEY, gap: int | None = None, pad: int | None = None, frame: bool = False):
    """The board as a sharp picture: every key is drawn at `key` pixels, with rounded corners, on a dark panel.
    frame=True adds the deck's body around the keys (for the README)."""
    from PIL import Image, ImageDraw

    from decktate.ui.render import render_key, snake, TEXT

    gap = key // 6 if gap is None else gap
    pad = (key // 3 if frame else gap) if pad is None else pad
    cols, rows = 5, 3
    now = board.clock()
    keys, prog = board.keys(), board.confirm_progress(now)
    W, H = cols * key + (cols - 1) * gap + 2 * pad, rows * key + (rows - 1) * gap + 2 * pad
    ss = 2  # the panel and the corner masks are drawn larger and scaled down, for smooth edges
    panel = Image.new("RGBA", (W * ss, H * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(panel)
    if frame:
        d.rounded_rectangle([0, 0, W * ss - 1, H * ss - 1], radius=int(key * 0.3 * ss), fill=(16, 17, 22, 255),
                            outline=(46, 49, 60, 255), width=2 * ss)
    else:
        d.rectangle([0, 0, W * ss, H * ss], fill=(12, 13, 17, 255))
    panel = panel.resize((W, H), Image.LANCZOS)
    mask = Image.new("L", (key * ss, key * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, key * ss - 1, key * ss - 1], radius=int(key * 0.14 * ss), fill=255)
    mask = mask.resize((key, key), Image.LANCZOS)
    for index in range(cols * rows):
        k = keys.get(index)
        img = render_key(k.face if k else None, (key, key), now)
        if prog and prog[0] == index:
            img = snake(img, prog[1], k.colour if k else TEXT)
        x, y = pad + (index % cols) * (key + gap), pad + (index // cols) * (key + gap)
        panel.paste(img.convert("RGBA"), (x, y), mask)
    return panel


def main(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    r = Rig(Path(tempfile.mkdtemp()), hold_s=30)
    paths = []

    def shot(name):
        r.board.tick()
        p = out / f"{name}.png"
        picture(r.board).save(p, optimize=True)
        paths.append(p)
        if name == "2-permission":  # the picture at the top of the README: larger keys, in the deck's body
            hero = out.parent / "decktate.png"
            picture(r.board, key=200, frame=True).save(hero, optimize=True)
            paths.append(hero)

    h = r.hub
    shot("0-empty")
    for i, (sid, name, mode) in enumerate((("s1", "api-server", "default"), ("s2", "web-app", "acceptEdits"), ("s3", "docs", "plan"),
                                           ("s4", "infra", "default"), ("s5", "mobile", "default"))):
        h.clock = lambda i=i: 1000.0 + i
        h.handle({"hook_event_name": "SessionStart", "session_id": sid, "cwd": f"/work/{name}", "permission_mode": mode})
    h.clock = lambda: 2000.0
    h.select("s1")
    h.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
    h.handle({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash", "tool_input": {}})
    h.handle({"hook_event_name": "Stop", "session_id": "s2", "last_assistant_message": "All tests pass."})
    h.statusline({"session_id": "s1", "model": {"display_name": "Opus 5"}, "version": "2.1.326", "effort": {"level": "high"},
                  "cost": {"total_cost_usd": 12.34, "total_lines_added": 120, "total_lines_removed": 30, "total_duration_ms": 754000},
                  "context_window": {"used_percentage": 91},
                  "rate_limits": {"five_hour": {"used_percentage": 81, "resets_at": time.time() + 7800},
                                  "seven_day": {"used_percentage": 44}}})
    shot("1-main")
    t, box = r.post(BASH)
    shot("2-permission")
    r.deck.press(7)
    r.advance(1.3)
    shot("2-always-confirming")
    r.deck.release(7)
    r.tap(6)
    t.join(1)
    r.advance(3)
    r.tap(3)
    shot("3-more-sessions")
    r.tap(9)
    r.tap(14)
    shot("3-commands")
    r.tap(9)
    r.tap(9)
    shot("4-stats")
    r.tap(9)
    r.tap(4)
    shot("4-settings")
    for n, topic in enumerate(("sound", "microphone", "voice", "display")):
        r.tap(n)
        shot(f"4-settings-{topic}")
        r.tap(9)
    r.tap(9)
    t, box = r.post(QUESTION)
    shot("5-menu")
    r.tap(1)
    r.tap(1), r.tap(2)
    shot("5-menu-multi")
    r.tap(4)
    t.join(1)
    r.advance(3)
    r.deck.press(10)
    r.advance(0.8)
    shot("6-dictating")
    r.deck.release(10)
    r.board.tick()
    shot("6-sent")
    from decktate.claude.discover import Running

    r.advance(3)
    r.running = [Running("x1", 501, "/work/legacy-importer", started=10.0), Running("x2", 502, "/work/api-server", started=20.0)]
    r.advance(3.1)
    r.tap(3)
    shot("7-all-sessions")
    r.hold(1, 0.7)
    shot("7-session-menu")
    return paths


if __name__ == "__main__":
    for p in main(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screens")):
        print(p)
