"""Audit log: every decision made from the deck, one JSON object per line.
Commands, reasons and answers can contain secrets, so every text is redacted before it is written,
and the file is readable by its owner only."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .redact import redact


def _clean(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {(redact(k) if isinstance(k, str) else k): _clean(v) for k, v in value.items()}  # keys too: a question's text
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


class Audit:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock

    def write(self, what: str, session: str, **detail) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())), "what": what,
               "session": session, **_clean(detail)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            if os.fstat(f.fileno()).st_mode & 0o077:  # a log from before this was private
                os.fchmod(f.fileno(), 0o600)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
