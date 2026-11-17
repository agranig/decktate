"""Pillow renderer. Everything is drawn at 4x and scaled down, so rings,
pictograms and sparklines come out anti-aliased on the 72 px keys."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..status import Series, Status
from . import icons

SS = 4  # supersampling factor
COLORS = {
    Status.OK: (52, 211, 153),
    Status.WARN: (251, 191, 36),
    Status.FAIL: (248, 81, 96),
    Status.RUNNING: (96, 165, 250),
    Status.UNKNOWN: (120, 128, 145),
    Status.IDLE: (74, 80, 98),
}
TINT = {Status.FAIL: (0.62, 0.26), Status.WARN: (0.40, 0.14), Status.RUNNING: (0.34, 0.10), Status.OK: (0.26, 0.07)}
BG_TOP, BG_BOTTOM = (23, 25, 34), (9, 10, 14)
TEXT, MUTED = (245, 247, 250), (146, 152, 168)
ACCENT = (167, 139, 250)  # navigation / context
PROD = (255, 59, 48)

_FONTS = [  # (path, weight is a variation name)
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


@lru_cache(maxsize=None)
def font(size: int, weight: str = "Bold") -> ImageFont.ImageFont:
    for path in _FONTS:
        if Path(path).exists():
            f = ImageFont.truetype(path, size)
            try:
                f.set_variation_by_name(weight)
            except Exception:
                pass
            return f
    return ImageFont.load_default(size)


@dataclass
class Face:
    """Declarative description of one key."""

    kind: str = "stat"  # stat | ring | icon | spark | text | stats | bars | session
    status: Status | None = None
    value: str = ""  # the one big figure
    label: str = ""  # tiny caption above
    caption: str = ""  # caption below the figure
    foot: str = ""  # tiny line at the very bottom (e.g. a time)
    icon: str = ""  # pictogram name (see icons.ICONS)
    ring: float | None = None  # 0..1 donut
    spark: list[float] = field(default_factory=list)
    threshold: float | None = None
    strip: list[Status] = field(default_factory=list)  # last runs, oldest first
    solid: bool = False  # tint the whole key with the status colour
    frame: bool = False  # prod
    blink: bool = False  # unacknowledged change
    pulse: bool = False  # running / attention
    disabled: bool = False
    accent: tuple[int, int, int] | None = None
    tint_value: bool = False  # draw the figure in the status colour (non-solid keys)
    text: str = ""  # kind "text": words wrapped over several lines (menu options, transcripts); kind "session": the name
    outline: tuple[int, int, int] | None = None  # coloured outline, e.g. the selected session
    stats: dict | None = None  # kind "stats": {"h5": pct, "d7": pct, "ctx": pct, "cost": usd}; any may be None


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


@lru_cache(maxsize=256)
def _gradient(w: int, h: int, tint=None, strength=(0.0, 0.0)) -> Image.Image:
    col = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        base = _mix(BG_TOP, BG_BOTTOM, t)
        if tint:
            base = _mix(base, tint, strength[0] + (strength[1] - strength[0]) * t)
        col.putpixel((0, y), base)
    return col.resize((w, h))


def _background(w: int, h: int, tint=None, strength=(0.0, 0.0)) -> Image.Image:
    return _gradient(int(w), int(h), tint, (round(strength[0], 2), round(strength[1], 2))).copy()


def _text(d, xy, text, size, colour, weight="Bold", anchor="mm", max_w=None, tracking=0):
    f = font(int(size), weight)
    if max_w:
        while size > 8 * SS and d.textlength(text, font=f) > max_w:
            size -= SS
            f = font(int(size), weight)
        if d.textlength(text, font=f) > max_w:
            while text and d.textlength(text + "…", font=f) > max_w:
                text = text[:-1]
            text += "…"
    d.text(xy, text, font=f, fill=colour, anchor=anchor)


def _wrap_label(d, text: str, size: float, max_w: float) -> list[str]:
    """One line if it fits, else two lines split at the space or hyphen that balances them."""
    import re

    f = font(int(size), "Semibold")
    parts = [p for p in re.split(r"(\s+|(?<=-))", text) if p]  # words; a hyphen stays on its word
    words = [p for p in parts if not p.isspace()]
    if d.textlength(text, font=f) <= max_w or len(words) < 2:
        return [text]

    def join(ws):
        out = ""
        for w in ws:
            out += w if not out or out.endswith("-") else " " + w
        return out

    best = min(range(1, len(words)), key=lambda i: max(d.textlength(join(words[:i]), font=f),
                                                       d.textlength(join(words[i:]), font=f)))
    return [join(words[:best]), join(words[best:])]


def pct_colour(pct) -> tuple:
    """Green below 70 %, amber from 70, red from 90."""
    if pct is None:
        return MUTED
    return COLORS[Status.FAIL] if pct >= 90 else COLORS[Status.WARN] if pct >= 70 else COLORS[Status.OK]


def _stats_summary(d, st: dict, W: float, H: float, u: float) -> None:
    """Four rows, one size:  5h [bar] 81%  /  7d [bar] 44%  /  ctx [bar] 91%  /  cost $12.34"""
    left, right = 5 * u, W - 5 * u
    f = font(int(11 * u), "Semibold")
    rows = [("5h", st.get("h5")), ("7d", st.get("d7")), ("ctx", st.get("ctx"))]
    label_w = max(d.textlength(name, font=f) for name, _ in rows) + 4 * u
    value_w = d.textlength("100%", font=f) + 3 * u
    step = H / 4
    for n, (name, v) in enumerate(rows):
        y = step * (n + 0.5)
        colour = pct_colour(v)
        d.text((left, y), name, font=f, fill=MUTED, anchor="lm")
        bx0, bx1 = left + label_w, right - value_w
        d.rounded_rectangle([bx0, y - 2.5 * u, bx1, y + 2.5 * u], radius=2.5 * u, fill=(44, 48, 62))
        if v:
            d.rounded_rectangle([bx0, y - 2.5 * u, bx0 + max(5 * u, (bx1 - bx0) * min(1.0, v / 100)), y + 2.5 * u],
                                radius=2.5 * u, fill=colour)
        d.text((right, y), "–" if v is None else f"{v:.0f}%", font=f, fill=colour, anchor="rm")
    cost = st.get("cost")
    y = step * 3.5
    d.text((left, y), "cost", font=f, fill=MUTED, anchor="lm")
    d.text((right, y), "–" if cost is None else f"${cost:.2f}" if cost < 1000 else f"${cost:.0f}", font=f, fill=TEXT, anchor="rm")


def _level_bars(d, levels: list[float], cx: float, top: float, bottom: float, W: float, u: float, colour) -> None:
    """Microphone level as a row of bars, newest on the right: visible proof that sound is arriving."""
    n = 9
    levels = ([0.0] * n + list(levels))[-n:]
    gap = (W - 16 * u) / n
    mid, half = (top + bottom) / 2, (bottom - top) / 2
    for i, v in enumerate(levels):
        x = 8 * u + gap * (i + 0.5)
        h = max(1.6 * u, half * min(1.0, v) ** 0.6)  # quiet speech should still move the bars
        icons.rline(d, [(x, mid - h), (x, mid + h)], colour if v > 0.03 else (70, 76, 96), 3.2 * u)


def wrap_words(d, text: str, f, max_w: float) -> list[str]:
    """Greedy word wrap; a word longer than the line is split."""
    lines, cur = [], ""
    for word in text.split():
        while d.textlength(word, font=f) > max_w and len(word) > 1:  # hard-split very long words
            cut = len(word)
            while cut > 1 and d.textlength(word[:cut] + "-", font=f) > max_w:
                cut -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:cut] + "-")
            word = word[cut:]
        trial = f"{cur} {word}".strip()
        if cur and d.textlength(trial, font=f) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


def _paragraph(d, text: str, cx: float, top: float, bottom: float, max_w: float, u: float, colour) -> None:
    """Centre wrapped text in the area, in the largest size at which it fits."""
    text = (text or "").strip()
    if not text:
        return
    sizes = (15, 13, 12, 11, 10, 9, 8)
    for size in sizes:
        f = font(int(size * u), "Semibold")
        lines = wrap_words(d, text, f, max_w)
        line_h = size * u * 1.18
        whole = all(d.textlength(w, font=f) <= max_w for w in text.split())  # shrink before splitting a word
        if (len(lines) * line_h <= bottom - top + 2 * u and whole) or size == sizes[-1]:
            break
    room = max(1, int((bottom - top + 2 * u) // line_h))
    if len(lines) > room:  # keep the end: for a running transcript the newest words matter
        lines = lines[-room:]
        lines[0] = "…" + lines[0]
    y = (top + bottom) / 2 - (len(lines) - 1) * line_h / 2
    for line in lines:
        d.text((cx, y), line, font=f, fill=colour, anchor="mm")
        y += line_h


def _name_lines(d, name: str, f, max_w: float, split: bool) -> list[str] | None:
    """Wrap a session name; it may break after - _ . / and at spaces. A part that is wider than the key
    is cut in two if `split`, otherwise the name does not fit (None)."""
    import re

    lines, cur = [], ""
    for part in [p for p in re.split(r"(?<=[-_./ ])", name.strip()) if p]:
        if d.textlength((cur + part).rstrip(), font=f) <= max_w:
            cur += part
            continue
        if cur.strip():
            lines.append(cur.rstrip())
        cur = part
        while d.textlength(cur.rstrip(), font=f) > max_w:
            if not split:
                return None
            cut = len(cur) - 1
            while cut > 1 and d.textlength(cur[:cut], font=f) > max_w:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    return lines + ([cur.rstrip()] if cur.strip() else [])


def _session(d, face: "Face", W: float, H: float, u: float, colour) -> None:
    """A session key: the name as large as the key allows (up to three lines), and one line
    underneath with a small pictogram and a word for what the session is doing."""
    cx, top, bottom, max_w = W / 2, 5 * u, H - 20 * u, W - 8 * u

    def fit(split: bool):
        for size in (18, 16, 14, 13, 12, 11, 10, 9):
            f = font(int(size * u), "Bold")
            lines = _name_lines(d, face.text, f, max_w, split)
            if lines and len(lines) * size * u * 1.12 <= bottom - top:
                return size, f, lines
        return None

    whole, cut = fit(False), fit(True)
    # cutting a long word in two is worth it only if that makes the name clearly bigger
    best = whole if whole and (whole[0] >= 13 or not cut or cut[0] < whole[0] + 3) else cut
    if best is None:  # still too long: smallest size, as many lines as fit, the rest dropped
        f = font(int(9 * u), "Bold")
        lines = (_name_lines(d, face.text, f, max_w, True) or [""])[:int((bottom - top) // (9 * u * 1.12))]
        lines[-1] = lines[-1][:-1] + "…"
        best = (9, f, lines)
    size, f, lines = best
    line_h = size * u * 1.12
    y = (top + bottom) / 2 - (len(lines) - 1) * line_h / 2
    for line in lines:
        d.text((cx, y), line, font=f, fill=MUTED if face.status == Status.UNKNOWN else TEXT, anchor="mm")  # grey: not attached
        y += line_h
    # the state line: pictogram + word, in the state's colour
    word, sf = face.value, font(int(10.5 * u), "Semibold")
    while len(word) > 3 and d.textlength(word, font=sf) > max_w - 14 * u:
        word = word[:-1]
    if word != face.value:
        word = word[:-1] + "…"
    tw, iw, y = d.textlength(word, font=sf), 11 * u, H - 10.5 * u
    x = cx - (tw + (iw + 3 * u if face.icon else 0)) / 2
    scol = TEXT if face.solid else MUTED if face.status in (None, Status.IDLE, Status.UNKNOWN) else colour
    if face.icon:
        icons.draw(d, face.icon, x + iw / 2, y, iw, scol, 1.5 * u)
        x += iw + 3 * u
    d.text((x, y), word, font=sf, fill=scol, anchor="lm")


def _sparkline(d, img, box, pts, colour, threshold=None):
    x0, y0, x1, y1 = box
    lo, hi = min(pts), max(pts)
    if threshold is not None:
        lo, hi = min(lo, threshold), max(hi, threshold)
    span = (hi - lo) or 1.0
    xy = [(x0 + (x1 - x0) * i / max(1, len(pts) - 1), y1 - (y1 - y0) * (v - lo) / span) for i, v in enumerate(pts)]
    if len(xy) == 1:
        xy = [(x0, xy[0][1]), (x1, xy[0][1])]
    # soft area fill under the line
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(xy + [(x1, y1 + 2 * SS), (x0, y1 + 2 * SS)], fill=70)
    fade = Image.linear_gradient("L").resize((img.width, int(y1 - y0 + 2 * SS)))
    fade = Image.eval(fade, lambda v: 255 - v)
    grad = Image.new("L", img.size, 0)
    grad.paste(fade, (0, int(y0)))
    img.paste(Image.new("RGB", img.size, colour), (0, 0), Image.composite(grad, Image.new("L", img.size, 0), mask).point(lambda v: v * 0.45))
    if threshold is not None:
        ty = y1 - (y1 - y0) * (threshold - lo) / span
        for x in range(int(x0), int(x1), 6 * SS):
            d.line([x, ty, x + 3 * SS, ty], fill=(255, 255, 255, 70) if img.mode == "RGBA" else (90, 96, 112), width=SS)
    icons.rline(d, xy, colour, 2 * SS)
    lx, ly = xy[-1]
    d.ellipse([lx - 3 * SS, ly - 3 * SS, lx + 3 * SS, ly + 3 * SS], fill=TEXT)


def fmt(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:.0f}"
    return f"{v:.1f}" if abs(v) >= 10 else f"{v:.2f}".rstrip("0").rstrip(".")


_CACHE: dict = {}


def render_key(face: Face | None, size=(72, 72), phase: float = 0.0) -> Image.Image:
    """phase: seconds on the animation clock (blink / pulse). Results are cached
    (animated faces per 1/12 s step of their cycle); treat the image as read-only."""
    if face is None:
        return Image.new("RGB", size, "black")
    animated = face.blink or (face.pulse and face.solid)  # (level bars change with their data, not with time)
    step = int((phase % (math.tau / (5.0 if face.blink else 2.6))) * 12) if animated else 0
    key = (repr(face), size, step)
    img = _CACHE.get(key)
    if img is None:
        if len(_CACHE) > 600:
            _CACHE.clear()
        img = _CACHE[key] = _render_key(face, size, step / 12 if animated else 0.0)
    return img


def _render_key(face: Face, size, phase: float) -> Image.Image:
    W, H = size[0] * SS, size[1] * SS
    u = W / 72  # one logical pixel
    colour = face.accent or (COLORS[face.status] if face.status else TEXT)
    breathe = 0.5 + 0.5 * math.sin(phase * (5.0 if face.blink else 2.6))
    tint = TINT.get(face.status, (0, 0)) if face.solid and face.status else (0, 0)
    if face.blink:
        tint = (min(0.9, tint[0] + 0.30 * breathe), tint[1] + 0.25 * breathe)
        if not face.solid:
            tint = (0.38 * breathe, 0.12 * breathe)
    elif face.pulse and face.solid:
        tint = (tint[0] * (0.65 + 0.5 * breathe), tint[1])
    if face.kind == "session" and face.outline and not (face.solid or face.blink):
        img = _background(W, H, face.outline, (0.34, 0.12))  # the session you are talking to stands out
    else:
        img = _background(W, H, colour if (face.solid or face.blink) and face.status else None, tint)
    d = ImageDraw.Draw(img)
    cx = W / 2

    top = 17 * u if face.label else 6 * u
    if face.label and face.kind != "spark":
        lcol = MUTED if not face.solid else _mix(TEXT, colour, 0.25)
        lines = _wrap_label(d, face.label.upper(), 9 * u, W - 8 * u) if face.kind == "stat" else None
        if lines and len(lines) == 2:  # long names (tests): two lines instead of an ellipsis
            _text(d, (cx, 9 * u), lines[0], 8.5 * u, lcol, "Semibold", max_w=W - 6 * u)
            _text(d, (cx, 19 * u), lines[1], 8.5 * u, lcol, "Semibold", max_w=W - 6 * u)
            top = 26 * u
        else:
            _text(d, (cx, 10 * u), face.label.upper(), 9 * u, lcol, "Semibold", max_w=W - 10 * u)
    bottom = H - 6 * u
    if face.strip:
        n = len(face.strip)
        gap = 9 * u
        x = cx - gap * (n - 1) / 2
        y = H - 9 * u
        for s in face.strip:
            r = 2.8 * u
            if s == Status.RUNNING:
                d.ellipse([x - r, y - r, x + r, y + r], outline=COLORS[s], width=int(1.2 * u))
            else:
                d.ellipse([x - r, y - r, x + r, y + r], fill=COLORS[s])
            x += gap
        bottom = y - 6 * u
    elif face.caption:
        lift = 11 * u if face.foot else 0
        _text(d, (cx, H - 11 * u - lift), face.caption, 12 * u, _mix(TEXT, MUTED, 0.25) if not face.solid else TEXT,
              "Semibold", max_w=W - 8 * u)
        bottom = H - 20 * u - lift
        if face.foot:
            _text(d, (cx, H - 8 * u), face.foot, 9 * u, MUTED if not face.solid else _mix(TEXT, colour, 0.3),
                  "Medium", max_w=W - 8 * u)
    mid = (top + bottom) / 2

    if face.kind == "ring":
        r = min((bottom - top) / 2, 25 * u)
        wd = 5 * u
        icons.arc(d, cx, mid, r, 0, 360, (44, 48, 62), wd)
        frac = face.ring if face.ring is not None else 0.0
        if frac > 0.004:
            icons.arc(d, cx, mid, r, 0, max(1.0, 360 * min(1.0, frac)), colour, wd)
        if face.icon:
            icons.draw(d, face.icon, cx, mid, r * 0.95, TEXT, 2.2 * u)
        else:
            _text(d, (cx, mid), face.value, 17 * u, TEXT, "Bold", max_w=2 * r - wd * 2.6)
    elif face.kind == "stats":
        _stats_summary(d, face.stats or {}, W, H, u)
    elif face.kind == "session":
        _session(d, face, W, H, u, colour)
    elif face.kind == "bars":
        _level_bars(d, face.spark, cx, top + 2 * u, bottom - 2 * u, W, u, colour)
    elif face.kind == "text":
        _paragraph(d, face.text, cx, top, bottom, W - 8 * u, u, TEXT if face.status is None or face.solid else colour)
    elif face.kind == "icon":
        if face.icon:
            icons.draw(d, face.icon, cx, mid, min(bottom - top, 34 * u) * 0.86, colour, 2.6 * u)
    elif face.kind == "spark":
        _text(d, (7 * u, 10 * u), face.label.upper(), 9 * u, MUTED, "Semibold", anchor="lm")
        if face.value:
            _text(d, (W - 7 * u, 10 * u), face.value, 12 * u, colour, "Bold", anchor="rm")
        if face.spark:
            _sparkline(d, img, (8 * u, 26 * u, W - 9 * u, H - 12 * u), face.spark, colour, face.threshold)
        else:
            _text(d, (cx, H / 2 + 6 * u), "–", 22 * u, MUTED)
    else:  # stat
        if face.icon and face.value:
            icons.draw(d, face.icon, cx, top + 9 * u, 15 * u, _mix(TEXT, colour, 0.15) if face.solid else colour, 1.9 * u)
            _text(d, (cx, (top + 18 * u + bottom) / 2), face.value, 26 * u, TEXT, "Bold", max_w=W - 10 * u)
        elif face.value:
            _text(d, (cx, mid), face.value, min(34 * u, (bottom - top) * 1.05),
                  colour if face.tint_value and not face.solid else TEXT, "Bold", max_w=W - 10 * u)
        elif face.icon:
            icons.draw(d, face.icon, cx, mid, min(bottom - top, 34 * u) * 0.86, colour, 2.6 * u)
        if face.status and not face.strip and not face.solid and not face.caption:
            y = H - 4.5 * u
            icons.rline(d, [(cx - 9 * u, y), (cx + 9 * u, y)], colour, 2.4 * u)

    if face.frame:
        d.rounded_rectangle([1.2 * u, 1.2 * u, W - 1.2 * u, H - 1.2 * u], radius=9 * u, outline=PROD, width=int(2 * u))
    if face.outline:
        d.rounded_rectangle([1.2 * u, 1.2 * u, W - 1.2 * u, H - 1.2 * u], radius=9 * u, outline=face.outline,
                            width=int(2.4 * u))
    out = img.resize(size, Image.LANCZOS)
    if face.disabled:
        out = Image.blend(out, Image.new("RGB", size, BG_BOTTOM), 0.68)
    return out


def _perimeter(w, h, inset, radius, n=220):
    """Points along a rounded rectangle, clockwise from the top centre."""
    x0, y0, x1, y1 = inset, inset, w - inset, h - inset
    r = radius
    segs = [("l", (w / 2, y0), (x1 - r, y0)), ("a", (x1 - r, y0 + r), 270), ("l", (x1, y0 + r), (x1, y1 - r)),
            ("a", (x1 - r, y1 - r), 0), ("l", (x1 - r, y1), (x0 + r, y1)), ("a", (x0 + r, y1 - r), 90),
            ("l", (x0, y1 - r), (x0, y0 + r)), ("a", (x0 + r, y0 + r), 180), ("l", (x0 + r, y0), (w / 2, y0))]
    lengths = [math.dist(s[1], s[2]) if s[0] == "l" else math.pi * r / 2 for s in segs]
    total = sum(lengths)
    pts = []
    for i in range(n + 1):
        dist = total * i / n
        for seg, ln in zip(segs, lengths):
            if dist <= ln or seg is segs[-1]:
                t = min(1.0, dist / ln) if ln else 0
                if seg[0] == "l":
                    pts.append((seg[1][0] + (seg[2][0] - seg[1][0]) * t, seg[1][1] + (seg[2][1] - seg[1][1]) * t))
                else:
                    a = math.radians(seg[2] + 90 * t)
                    pts.append((seg[1][0] + r * math.cos(a), seg[1][1] + r * math.sin(a)))
                break
            dist -= ln
    return pts


def snake(img: Image.Image, p: float, colour=TEXT) -> Image.Image:
    """Hold-to-confirm: an outline that runs around the key and closes at p = 1."""
    w, h = img.size
    W, H = w * SS, h * SS
    u = W / 72
    base = Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), 0.35)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pts = _perimeter(W, H, 3.2 * u, 9 * u)
    d.line(pts, fill=(255, 255, 255, 36), width=int(3.5 * u), joint="curve")
    n = max(2, int(len(pts) * max(0.0, min(1.0, p))))
    icons.rline(d, pts[:n], colour + (255,), 4.2 * u)
    hx, hy = pts[n - 1]
    d.ellipse([hx - 3.6 * u, hy - 3.6 * u, hx + 3.6 * u, hy + 3.6 * u], fill=(255, 255, 255, 255))
    layer = layer.resize(img.size, Image.LANCZOS)
    base.paste(layer, (0, 0), layer)
    return base


def split_canvas(canvas: Image.Image, cols=5, rows=2, size=(72, 72)) -> list[Image.Image]:
    """Cut a merged canvas into row-major key tiles."""
    w, h = size
    if canvas.size != (cols * w, rows * h):
        canvas = canvas.resize((cols * w, rows * h), Image.LANCZOS)
    return [canvas.crop((c * w, r * h, (c + 1) * w, (r + 1) * h)) for r in range(rows) for c in range(cols)]


def tile_text(d, col, row, text, size, colour=TEXT, weight="Bold", dy=0, k=72 * SS):
    """Centre text inside one key of a merged canvas, so key gaps never cut it."""
    _text(d, (col * k + k / 2, row * k + k / 2 + dy * SS), text, size * SS, colour, weight, max_w=k - 8 * SS)


def render_chart(series: Series | None, title: str, cols=5, rows=2, k=72) -> Image.Image:
    """One metric across the merged content keys."""
    W, H = cols * k * SS, rows * k * SS
    u = SS
    img = _background(W, H)
    d = ImageDraw.Draw(img)
    if not series or not series.points:
        tile_text(d, cols // 2, 0, "no data" if series else "…", 16, MUTED, dy=36)
        return img.resize((cols * k, rows * k), Image.LANCZOS)
    colour = COLORS[series.status]
    _text(d, (10 * u, 14 * u), series.label, 13 * u, MUTED, "Semibold", anchor="lm")
    _text(d, (10 * u, 38 * u), fmt(series.last) + (f" {series.unit}" if series.unit else ""), 24 * u, colour, anchor="lm")
    _text(d, (W - 10 * u, 14 * u), title, 11 * u, MUTED, "Medium", anchor="rm", max_w=k * 2.6 * u)
    _text(d, (W - 10 * u, 34 * u), f"{fmt(min(series.points))} – {fmt(max(series.points))}", 11 * u, MUTED, "Medium", anchor="rm")
    _sparkline(d, img, (10 * u, 60 * u, W - 12 * u, H - 14 * u), series.points, colour, series.threshold)
    return img.resize((cols * k, rows * k), Image.LANCZOS)


def test_pattern(index: int, total: int, size=(72, 72)) -> Image.Image:
    """Hue ramp, key index, corner marks (reveals flips / rotation)."""
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(index / total, 0.75, 0.85)
    img = Image.new("RGB", size, (int(r * 255), int(g * 255), int(b * 255)))
    d = ImageDraw.Draw(img)
    d.text((size[0] / 2, size[1] / 2), str(index), font=font(30), fill=TEXT, anchor="mm")
    d.polygon([(0, 0), (14, 0), (0, 14)], fill=TEXT)
    d.text((size[0] - 3, size[1] - 2), "BR", font=font(10), fill=TEXT, anchor="rd")
    return img
