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


def speakable(text: str, max_sentences: int = 0, max_chars: int = 320) -> str:
    """A message as it can be read out: without code blocks, markup or secrets.
    max_sentences > 0 keeps only that many sentences (and at most max_chars); 0 keeps the whole message."""
    text = re.sub(r"```.*?```", "\n(code)\n", text or "", flags=re.S)
    text = re.sub(r"^[ \t]*\|?[ \t:|\-]*-{3,}[ \t:|\-]*\|?[ \t]*$", "", text, flags=re.M)   # the rule under a table's head
    text = re.sub(r"^[ \t]*\|(.*)\|[ \t]*$", lambda m: ", ".join(c.strip() for c in m.group(1).split("|")), text,
                  flags=re.M)                                                                  # table rows
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)          # links -> their text
    text = re.sub(r"^\s*[#>*\-]+\s*", "", text, flags=re.M)        # headings, bullets, quotes
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"(?<![\w.])(?:~|\.{0,2})?/[\w./\-]{12,}", lambda m: m.group(0).rstrip("/").split("/")[-1], text)
    # a heading, a bullet or a table row is a line of its own: it must not run into the next one when read out
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = " ".join(ln if ln[-1] in ".!?:;," or n == len(lines) - 1 else ln + "." for n, ln in enumerate(lines))
    text = redact(re.sub(r"\s+", " ", text).strip())
    if max_sentences <= 0:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(sentences[:max_sentences]).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0] + "."
    return out


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
