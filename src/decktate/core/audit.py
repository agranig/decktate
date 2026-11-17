"""Audit log: every decision made from the deck, one JSON object per line."""

from __future__ import annotations

import json
import time
from pathlib import Path


class Audit:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock = Path(path), clock

    def write(self, what: str, session: str, **detail) -> None:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())), "what": what,
               "session": session, **detail}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
