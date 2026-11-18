"""Redaction: text sent to a speech service leaves the machine, so secrets
are replaced before anything is spoken."""

from __future__ import annotations

import re

_PATTERNS = [
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}\b"), "a secret key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "a GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"), "a Slack token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS key"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}\b"), "a token"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}"), r"\1 credentials"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "a private key"),
    # NAME=value where the name looks sensitive (env assignments, CLI flags, URLs)
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)[A-Z0-9_]*)\s*[=:]\s*"
                r"(\"[^\"]*\"|'[^']*'|[^\s&;]+)"), r"\1 redacted"),
    (re.compile(r"(?i)(--(?:password|token|secret|api-key)[= ])\S+"), r"\1redacted"),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://redacted@"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "a long hex value"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])"), "a long encoded value"),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_STRUCTURE = re.compile(r"(#{1,6}\s+|>+\s*|[-*+]\s+|\d+[.)]\s+)")  # heading, quote, bullet, numbered item
_TABLE_RULE = re.compile(r"\|?[ \t:|\-]*-{3,}[ \t:|\-]*\|?$")       # the rule under a table's head


def speakable(text: str, max_sentences: int = 0, max_chars: int = 320) -> str:
    """A message as it can be read out: without code blocks, markup or secrets.
    max_sentences > 0 keeps only that many sentences (and at most max_chars); 0 keeps the whole message."""
    # Secrets first, on the text as it came: a credential that wraps over two lines must be caught before
    # anything is put between those lines. (There is a second pass at the end, on the finished text.)
    text = redact(text or "")
    text = re.sub(r"(```|~~~).*?\1", "\n\n(code)\n\n", text, flags=re.S)   # fenced code, either kind of fence
    lines = text.split("\n")

    def structural(ln: str) -> bool:
        ln = ln.strip()
        return bool(_STRUCTURE.match(ln)) or (ln.startswith("|") and ln.endswith("|") and len(ln) > 1)

    out: list[tuple[str, bool]] = []  # (the line, a sentence ends here)
    for n, ln in enumerate(lines):
        raw = ln.strip()
        if not raw or _TABLE_RULE.fullmatch(raw):
            continue
        own = structural(raw)  # a heading, a bullet, a table row: a line of its own
        if raw.startswith("|") and raw.endswith("|") and len(raw) > 1:
            raw = ", ".join(c.strip() for c in raw.strip("|").split("|"))
        else:
            m = _STRUCTURE.match(raw)
            raw = raw[m.end():].strip() if m else raw
        if not raw:
            continue
        nxt = lines[n + 1] if n + 1 < len(lines) else ""
        # A sentence ends after a structural line, at the end of a paragraph, and before a structural line.
        # A line that merely wraps inside a paragraph runs on into the next one.
        out.append((raw, own or not nxt.strip() or structural(nxt)))
    text = " ".join(ln + "." if ends and ln[-1] not in ".!?:;," and n < len(out) - 1 else ln
                    for n, (ln, ends) in enumerate(out))
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)          # links -> their text
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"(?<![\w.])(?:~|\.{0,2})?/[\w./\-]{12,}", lambda m: m.group(0).rstrip("/").split("/")[-1], text)
    text = redact(re.sub(r"\s+", " ", text).strip())
    if max_sentences <= 0:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out_text = " ".join(sentences[:max_sentences]).strip()
    if len(out_text) > max_chars:
        out_text = out_text[:max_chars].rsplit(" ", 1)[0] + "."
    return out_text


def pieces(text: str, limit: int = 600) -> list[str]:
    """Split a long text at sentence ends into pieces of about `limit` characters, so that a voice can start
    with the first one right away instead of waiting for the whole text."""
    out, cur = [], ""
    for sentence in re.split(r"(?<=[.!?:;])\s+", (text or "").strip()):
        while len(sentence) > limit:  # one endless sentence: cut at a space
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                out.append(cur)
                cur = ""
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if cur and len(cur) + 1 + len(sentence) > limit:
            out.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()
    return out + ([cur] if cur else [])
