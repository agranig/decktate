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


def speakable(text: str, max_sentences: int = 2, max_chars: int = 320) -> str:
    """The first sentences of a message, without code blocks, markup or secrets."""
    text = re.sub(r"```.*?```", " (code) ", text or "", flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)          # links -> their text
    text = re.sub(r"^\s*[#>*\-]+\s*", "", text, flags=re.M)        # headings, bullets, quotes
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"(?<![\w.])(?:~|\.{0,2})?/[\w./\-]{12,}", lambda m: m.group(0).rstrip("/").split("/")[-1], text)
    text = redact(re.sub(r"\s+", " ", text).strip())
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(sentences[:max_sentences]).strip()
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0] + "."
    return out
