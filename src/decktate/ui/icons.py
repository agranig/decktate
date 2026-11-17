"""Line pictograms, drawn with Pillow primitives on the supersampled canvas.

Each icon draws inside a square of side `s` centred on (cx, cy); `w` is the
stroke width. All coordinates are already in supersampled pixels.
"""

from __future__ import annotations

import math

from PIL import ImageDraw


def rline(d: ImageDraw.ImageDraw, pts, colour, w):
    """Polyline with round caps and joins."""
    d.line(pts, fill=colour, width=int(w), joint="curve")
    r = w / 2
    for x, y in pts:
        d.ellipse([x - r, y - r, x + r, y + r], fill=colour)


def arc(d, cx, cy, r, a0, a1, colour, w):
    """Arc with round caps; angles in degrees, 0 = 12 o'clock, clockwise."""
    if a1 - a0 >= 359.9:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=colour, width=int(w))
        return
    d.arc([cx - r, cy - r, cx + r, cy + r], a0 - 90, a1 - 90, fill=colour, width=int(w))
    rr = r - w / 2
    for a in (a0, a1):
        x, y = cx + rr * math.sin(math.radians(a)), cy - rr * math.cos(math.radians(a))
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=colour)


def _home(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx - h, cy - h * 0.05), (cx, cy - h * 0.9), (cx + h, cy - h * 0.05)], c, w)
    rline(d, [(cx - h * 0.68, cy - h * 0.2), (cx - h * 0.68, cy + h * 0.85), (cx + h * 0.68, cy + h * 0.85),
              (cx + h * 0.68, cy - h * 0.2)], c, w)


def _chevron(dx, dy):
    def draw(d, cx, cy, s, c, w):
        h = s * 0.32
        if dx:
            rline(d, [(cx - dx * h * 0.5 + dx * h, cy - h), (cx - dx * h * 0.5, cy), (cx - dx * h * 0.5 + dx * h, cy + h)], c, w * 1.25)
        else:
            rline(d, [(cx - h, cy - dy * h * 0.5 + dy * h), (cx, cy - dy * h * 0.5), (cx + h, cy - dy * h * 0.5 + dy * h)], c, w * 1.25)
    return draw


def _filter(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx - h * 0.9, cy - h * 0.7), (cx + h * 0.9, cy - h * 0.7), (cx + h * 0.22, cy + h * 0.05),
              (cx + h * 0.22, cy + h * 0.8), (cx - h * 0.22, cy + h * 0.5), (cx - h * 0.22, cy + h * 0.05),
              (cx - h * 0.9, cy - h * 0.7)], c, w)


def _fail(d, cx, cy, s, c, w):
    r = s * 0.46
    arc(d, cx, cy, r, 0, 360, c, w)
    k = s * 0.16
    rline(d, [(cx - k, cy - k), (cx + k, cy + k)], c, w)
    rline(d, [(cx + k, cy - k), (cx - k, cy + k)], c, w)


def _pass(d, cx, cy, s, c, w):
    r = s * 0.46
    arc(d, cx, cy, r, 0, 360, c, w)
    rline(d, [(cx - s * 0.2, cy + s * 0.01), (cx - s * 0.05, cy + s * 0.16), (cx + s * 0.21, cy - s * 0.14)], c, w)


def _running(d, cx, cy, s, c, w):
    arc(d, cx, cy, s * 0.44, 40, 320, c, w)
    k = s * 0.13
    d.polygon([(cx - k * 0.8, cy - k * 1.3), (cx + k * 1.4, cy), (cx - k * 0.8, cy + k * 1.3)], fill=c)


def _play(d, cx, cy, s, c, w):
    k = s * 0.36
    pts = [(cx - k * 0.75, cy - k), (cx + k, cy), (cx - k * 0.75, cy + k)]
    d.polygon(pts, fill=c)
    rline(d, pts + pts[:1], c, w)


def _ffwd(d, cx, cy, s, c, w):
    """Two triangles pointing right, as Claude Code shows for "accept edits on" / "auto mode on"."""
    k = s * 0.3
    for x in (cx - k * 0.95, cx + k * 0.35):
        pts = [(x - k * 0.45, cy - k), (x + k * 0.75, cy), (x - k * 0.45, cy + k)]
        d.polygon(pts, fill=c)
        rline(d, pts + pts[:1], c, w * 0.7)


def _pause(d, cx, cy, s, c, w):
    """Two bars, as Claude Code shows for "plan mode on"."""
    k = s * 0.3
    for x in (cx - k * 0.55, cx + k * 0.55):
        d.rounded_rectangle([x - k * 0.3, cy - k, x + k * 0.3, cy + k], radius=k * 0.18, fill=c)


def _stop(d, cx, cy, s, c, w):
    k = s * 0.3
    d.rounded_rectangle([cx - k, cy - k, cx + k, cy + k], radius=k * 0.3, fill=c)


def _live(d, cx, cy, s, c, w):
    heights = [0.3, 0.75, 1.0, 0.55, 0.8, 0.35]
    step = s / len(heights)
    for i, hgt in enumerate(heights):
        x = cx - s / 2 + step * (i + 0.5)
        rline(d, [(x, cy - s * 0.42 * hgt), (x, cy + s * 0.42 * hgt)], c, w)


def _bot(d, cx, cy, s, c, w):
    h = s / 2
    d.rounded_rectangle([cx - h * 0.85, cy - h * 0.4, cx + h * 0.85, cy + h * 0.85], radius=h * 0.3, outline=c, width=int(w))
    rline(d, [(cx, cy - h * 0.4), (cx, cy - h * 0.78)], c, w)
    d.ellipse([cx - w, cy - h * 0.95 - w, cx + w, cy - h * 0.95 + w], fill=c)
    for ex in (-0.36, 0.36):
        d.ellipse([cx + ex * h - w * 0.9, cy + h * 0.08 - w * 0.9, cx + ex * h + w * 0.9, cy + h * 0.08 + w * 0.9], fill=c)
    rline(d, [(cx - h * 0.25, cy + h * 0.5), (cx + h * 0.25, cy + h * 0.5)], c, w * 0.8)


def _sip(d, cx, cy, s, c, w):
    """A handset: two blobs joined by an arc."""
    r = s * 0.36
    arc(d, cx + s * 0.06, cy - s * 0.06, r, 200, 340, c, w * 1.1)
    for a in (200, 340):
        x = cx + s * 0.06 + (r - w * 0.5) * math.sin(math.radians(a))
        y = cy - s * 0.06 - (r - w * 0.5) * math.cos(math.radians(a))
        d.ellipse([x - w * 1.5, y - w * 1.5, x + w * 1.5, y + w * 1.5], fill=c)
    for i, rr in enumerate((0.2, 0.36)):  # sound waves
        arc(d, cx + s * 0.1, cy - s * 0.1, s * rr, 20, 70, c, w * 0.8)


def _agents(d, cx, cy, s, c, w):
    h = s / 2
    for i in (-1, 0, 1):
        y = cy + i * h * 0.62
        d.rounded_rectangle([cx - h * 0.9, y - h * 0.23, cx + h * 0.9, y + h * 0.23], radius=h * 0.2, outline=c, width=int(w * 0.85))
        d.ellipse([cx - h * 0.62 - w * 0.6, y - w * 0.6, cx - h * 0.62 + w * 0.6, y + w * 0.6], fill=c)


def _warn(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx, cy - h * 0.85), (cx + h * 0.95, cy + h * 0.75), (cx - h * 0.95, cy + h * 0.75), (cx, cy - h * 0.85)], c, w)
    rline(d, [(cx, cy - h * 0.3), (cx, cy + h * 0.2)], c, w)
    d.ellipse([cx - w * 0.7, cy + h * 0.45 - w * 0.7, cx + w * 0.7, cy + h * 0.45 + w * 0.7], fill=c)


def _report(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx - h * 0.1, cy - h * 0.7), (cx - h * 0.75, cy - h * 0.7), (cx - h * 0.75, cy + h * 0.75),
              (cx + h * 0.7, cy + h * 0.75), (cx + h * 0.7, cy + h * 0.1)], c, w)
    rline(d, [(cx - h * 0.05, cy + h * 0.05), (cx + h * 0.8, cy - h * 0.8)], c, w)
    rline(d, [(cx + h * 0.3, cy - h * 0.8), (cx + h * 0.8, cy - h * 0.8), (cx + h * 0.8, cy - h * 0.3)], c, w)


def _bookmark(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx - h * 0.55, cy - h * 0.85), (cx + h * 0.55, cy - h * 0.85), (cx + h * 0.55, cy + h * 0.85),
              (cx, cy + h * 0.35), (cx - h * 0.55, cy + h * 0.85), (cx - h * 0.55, cy - h * 0.85)], c, w)


def _chart(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx - h * 0.9, cy + h * 0.5), (cx - h * 0.35, cy - h * 0.1), (cx + h * 0.1, cy + h * 0.25),
              (cx + h * 0.9, cy - h * 0.7)], c, w)
    rline(d, [(cx - h * 0.9, cy + h * 0.9), (cx + h * 0.9, cy + h * 0.9)], c, w * 0.7)


def _dots(d, cx, cy, s, c, w):
    for i in (-1, 0, 1):
        x = cx + i * s * 0.3
        d.ellipse([x - w * 1.1, cy - w * 1.1, x + w * 1.1, cy + w * 1.1], fill=c)


def _pulse(d, cx, cy, s, c, w):
    """Heartbeat line: monitoring."""
    h = s / 2
    rline(d, [(cx - h, cy), (cx - h * 0.45, cy), (cx - h * 0.2, cy - h * 0.75), (cx + h * 0.12, cy + h * 0.7),
              (cx + h * 0.4, cy - h * 0.25), (cx + h * 0.55, cy), (cx + h, cy)], c, w)


def _sort(d, cx, cy, s, c, w):
    """Three bars of decreasing length: order by."""
    h = s / 2
    for i, frac in enumerate((0.9, 0.55, 0.22)):
        y = cy + (i - 1) * h * 0.62
        rline(d, [(cx - h * 0.9, y), (cx - h * 0.9 + 2 * h * frac, y)], c, w * 1.1)


def _mic(d, cx, cy, s, c, w):
    h = s / 2
    d.rounded_rectangle([cx - h * 0.32, cy - h * 0.9, cx + h * 0.32, cy + h * 0.15], radius=h * 0.32, outline=c, width=int(w))
    arc(d, cx, cy - h * 0.05, h * 0.62, 90, 270, c, w)
    rline(d, [(cx, cy + h * 0.55), (cx, cy + h * 0.9)], c, w)
    rline(d, [(cx - h * 0.35, cy + h * 0.9), (cx + h * 0.35, cy + h * 0.9)], c, w)


def _speaker(d, cx, cy, s, c, w):
    h = s / 2
    pts = [(cx - h * 0.85, cy - h * 0.28), (cx - h * 0.4, cy - h * 0.28), (cx + h * 0.05, cy - h * 0.7),
           (cx + h * 0.05, cy + h * 0.7), (cx - h * 0.4, cy + h * 0.28), (cx - h * 0.85, cy + h * 0.28),
           (cx - h * 0.85, cy - h * 0.28)]
    rline(d, pts, c, w)
    arc(d, cx + h * 0.05, cy, h * 0.42, 45, 135, c, w)
    arc(d, cx + h * 0.05, cy, h * 0.8, 50, 130, c, w)


def _mute(d, cx, cy, s, c, w):
    h = s / 2
    pts = [(cx - h * 0.85, cy - h * 0.28), (cx - h * 0.4, cy - h * 0.28), (cx + h * 0.05, cy - h * 0.7),
           (cx + h * 0.05, cy + h * 0.7), (cx - h * 0.4, cy + h * 0.28), (cx - h * 0.85, cy + h * 0.28),
           (cx - h * 0.85, cy - h * 0.28)]
    rline(d, pts, c, w)
    rline(d, [(cx + h * 0.4, cy - h * 0.3), (cx + h * 0.9, cy + h * 0.3)], c, w)
    rline(d, [(cx + h * 0.9, cy - h * 0.3), (cx + h * 0.4, cy + h * 0.3)], c, w)


def _check(d, cx, cy, s, c, w):
    rline(d, [(cx - s * 0.36, cy + s * 0.02), (cx - s * 0.1, cy + s * 0.28), (cx + s * 0.38, cy - s * 0.26)], c, w * 1.5)


def _cross(d, cx, cy, s, c, w):
    k = s * 0.3
    rline(d, [(cx - k, cy - k), (cx + k, cy + k)], c, w * 1.5)
    rline(d, [(cx + k, cy - k), (cx - k, cy + k)], c, w * 1.5)


def _shield(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx, cy - h * 0.9), (cx + h * 0.75, cy - h * 0.55), (cx + h * 0.75, cy + h * 0.05), (cx, cy + h * 0.9),
              (cx - h * 0.75, cy + h * 0.05), (cx - h * 0.75, cy - h * 0.55), (cx, cy - h * 0.9)], c, w)
    rline(d, [(cx - h * 0.3, cy - h * 0.02), (cx - h * 0.05, cy + h * 0.22), (cx + h * 0.34, cy - h * 0.22)], c, w)


def _slash(d, cx, cy, s, c, w):
    rline(d, [(cx + s * 0.18, cy - s * 0.42), (cx - s * 0.18, cy + s * 0.42)], c, w * 1.4)


def _gear(d, cx, cy, s, c, w):
    """A cog: the outline of a wheel with six flat-topped teeth, and the hole in the middle."""
    teeth, outer, inner = 6, s * 0.5, s * 0.37
    pts = []
    for i in range(teeth):
        mid = i * 360 / teeth
        # root -> flank up -> flat tooth top -> flank down -> root (arc between teeth)
        for a, r in ((mid - 17, inner), (mid - 11, outer), (mid + 11, outer), (mid + 17, inner)):
            pts.append((cx + math.sin(math.radians(a)) * r, cy - math.cos(math.radians(a)) * r))
        for a in range(int(mid + 22), int(mid + 360 / teeth - 17), 5):
            pts.append((cx + math.sin(math.radians(a)) * inner, cy - math.cos(math.radians(a)) * inner))
    rline(d, pts + [pts[0]], c, w * 0.85)
    arc(d, cx, cy, s * 0.15, 0, 360, c, w * 0.85)


def _send(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx - h * 0.85, cy - h * 0.7), (cx + h * 0.9, cy), (cx - h * 0.85, cy + h * 0.7), (cx - h * 0.45, cy),
              (cx - h * 0.85, cy - h * 0.7)], c, w)
    rline(d, [(cx - h * 0.45, cy), (cx + h * 0.2, cy)], c, w)


def _question(d, cx, cy, s, c, w):
    h = s / 2
    arc(d, cx, cy - h * 0.35, h * 0.42, 270, 495, c, w * 1.2)
    rline(d, [(cx + h * 0.02, cy + h * 0.02), (cx, cy + h * 0.32)], c, w * 1.2)
    d.ellipse([cx - w, cy + h * 0.68 - w, cx + w, cy + h * 0.68 + w], fill=c)


def _pencil(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx - h * 0.8, cy + h * 0.8), (cx - h * 0.65, cy + h * 0.3), (cx + h * 0.45, cy - h * 0.8),
              (cx + h * 0.8, cy - h * 0.45), (cx - h * 0.3, cy + h * 0.65), (cx - h * 0.8, cy + h * 0.8)], c, w)


def _plan(d, cx, cy, s, c, w):
    h = s / 2
    d.rounded_rectangle([cx - h * 0.7, cy - h * 0.85, cx + h * 0.7, cy + h * 0.85], radius=h * 0.15, outline=c, width=int(w))
    for i in (-1, 0, 1):
        rline(d, [(cx - h * 0.38, cy + i * h * 0.4), (cx + h * 0.38, cy + i * h * 0.4)], c, w * 0.8)


def _bolt(d, cx, cy, s, c, w):
    h = s / 2
    rline(d, [(cx + h * 0.2, cy - h * 0.9), (cx - h * 0.45, cy + h * 0.1), (cx + h * 0.05, cy + h * 0.1),
              (cx - h * 0.2, cy + h * 0.9), (cx + h * 0.45, cy - h * 0.1), (cx - h * 0.05, cy - h * 0.1),
              (cx + h * 0.2, cy - h * 0.9)], c, w)


def _hand(d, cx, cy, s, c, w):
    """Manual mode: a raised hand (you decide)."""
    h = s / 2
    for i, top in enumerate((-0.55, -0.85, -0.9, -0.7)):
        x = cx + (i - 1.5) * h * 0.3
        rline(d, [(x, cy + h * 0.1), (x, cy + top * h)], c, w * 1.15)
    rline(d, [(cx - h * 0.45, cy + h * 0.1), (cx - h * 0.45, cy + h * 0.55), (cx - h * 0.1, cy + h * 0.9),
              (cx + h * 0.35, cy + h * 0.9), (cx + h * 0.45, cy + h * 0.5), (cx + h * 0.45, cy + h * 0.1)], c, w)


ICONS = {
    "mic": _mic, "speaker": _speaker, "mute": _mute, "check": _check, "cross": _cross, "shield": _shield,
    "slash": _slash, "gear": _gear, "send": _send, "question": _question, "pencil": _pencil, "plan": _plan,
    "bolt": _bolt, "hand": _hand,
    "sort": _sort, "ffwd": _ffwd, "pause": _pause,
    "pulse": _pulse,
    "home": _home, "back": _chevron(1, 0), "up": _chevron(0, 1), "down": _chevron(0, -1),
    "filter": _filter, "fail": _fail, "pass": _pass, "running": _running, "play": _play, "stop": _stop,
    "live": _live, "bot": _bot, "sip": _sip, "agents": _agents, "warn": _warn, "report": _report,
    "bookmark": _bookmark, "chart": _chart, "dots": _dots,
}


def draw(d: ImageDraw.ImageDraw, name: str, cx: float, cy: float, s: float, colour, w: float) -> None:
    ICONS[name](d, cx, cy, s, colour, w)
