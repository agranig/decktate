"""Dictation: microphone -> Deepgram live transcription (Nova-3, /v1/listen).

Push to talk: audio is streamed while the key is held; on release the stream
is finalized so the last words arrive without waiting for a pause.
"""

from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlencode

from ..config import Config
from .audio import MIC_RATE, Microphone

WS = "wss://api.deepgram.com/v1/listen"


def listen_url(cfg: Config) -> str:
    lang = cfg.speech.language
    params: list[tuple[str, str]] = [
        ("model", cfg.speech.stt_model), ("language", lang if lang in ("en", "de", "multi") else "en"),
        ("encoding", "linear16"), ("sample_rate", str(MIC_RATE)), ("channels", "1"),
        ("interim_results", "true"), ("smart_format", "true"),
        ("endpointing", "100" if lang == "multi" else "300"),
    ]
    if lang == "multi":
        params += [("language_hint", "en"), ("language_hint", "de")]
    params += [("keyterm", t) for t in cfg.speech.keyterms[:50]]  # repeated param; no commas, no weights
    return WS + "?" + urlencode(params)


def check_key(key: str) -> str:
    """'' if the Deepgram key works, else why not."""
    import httpx

    try:
        r = httpx.get("https://api.deepgram.com/v1/auth/token", headers={"Authorization": f"Token {key}"}, timeout=15)
    except httpx.HTTPError as exc:
        return type(exc).__name__
    return "" if r.status_code == 200 else ("invalid API key" if r.status_code in (401, 403) else f"HTTP {r.status_code}")


class Dictation:
    """One push-to-talk take. start() on key down, finish() on key up -> the text.

    Nothing that is said may get lost at either end:
      - start() only opens the microphone and returns; the connection to Deepgram
        is made in the background and the sound recorded meanwhile is sent once it is up.
      - finish() keeps recording for a moment (the last word is often still being
        spoken when the key comes up), sends everything that was recorded, and only
        then asks Deepgram for the final text.
    """

    TAIL = 0.3  # seconds of sound still recorded after the key is released

    def __init__(self, cfg: Config, mic: Microphone | None = None, connect=None):
        self.cfg = cfg
        self.mic = mic or Microphone(cfg.audio.input)
        self._connect = connect  # injectable for tests: url, headers -> connection with send / recv / close
        self.finals: list[str] = []
        self.interim = ""
        self.error = ""
        self.active = False
        self._ws = None
        self._worker: threading.Thread | None = None
        self._finalized = threading.Event()
        self._released = threading.Event()  # key is up and the microphone is closed: send the rest, then finalize
        self._cancelled = False
        self._wait = 3.0

    @property
    def enabled(self) -> bool:
        return bool(os.environ.get("DEEPGRAM_API_KEY")) or self._connect is not None

    @property
    def text(self) -> str:
        return " ".join(t for t in self.finals + [self.interim] if t).strip()

    @property
    def level(self) -> float:
        return self.mic.level

    @property
    def ready(self) -> bool:
        """The microphone delivers sound (it can take a moment to open, e.g. Bluetooth headsets)."""
        return self.active and getattr(self.mic, "frames", 1) > 0

    def start(self) -> bool:
        self.finals, self.interim, self.error = [], "", ""
        self._finalized.clear()
        self._released.clear()
        self._cancelled = False
        if not self.enabled:
            self.error = "no Deepgram key"
            return False
        if not self.mic.start():
            self.error = self.mic.error
            return False
        self.active = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        return True

    def _run(self) -> None:
        try:
            headers = {"Authorization": f"Token {os.environ.get('DEEPGRAM_API_KEY', '')}"}
            if self._connect:
                ws = self._connect(listen_url(self.cfg), headers)
            else:
                from websockets.sync.client import connect

                ws = connect(listen_url(self.cfg), additional_headers=headers, open_timeout=8)
        except Exception as exc:
            self.error = f"Deepgram: {type(exc).__name__}"
            return
        self._ws = ws
        threading.Thread(target=self._receive, daemon=True).start()
        try:
            while not self._cancelled:
                chunk = self.mic.read(0.1)
                if chunk:
                    ws.send(chunk)
                elif self._released.is_set():  # microphone closed and everything it recorded is sent
                    break
            if not self._cancelled:
                ws.send(json.dumps({"type": "Finalize"}))
                self._finalized.wait(self._wait)
                ws.send(json.dumps({"type": "CloseStream"}))
        except Exception as exc:
            self.error = self.error or f"Deepgram: {type(exc).__name__}"
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _receive(self) -> None:
        try:
            while True:
                msg = self._ws.recv()
                if isinstance(msg, (bytes, bytearray)):
                    continue
                data = json.loads(msg)
                if data.get("type") == "Results":
                    alt = (data.get("channel") or {}).get("alternatives") or [{}]
                    transcript = (alt[0].get("transcript") or "").strip()
                    if data.get("is_final"):
                        if transcript:
                            self.finals.append(transcript)
                        self.interim = ""
                    else:
                        self.interim = transcript
                    if data.get("from_finalize"):
                        self._finalized.set()
                elif data.get("type") == "Error":
                    self.error = data.get("description") or data.get("message") or "Deepgram error"
        except Exception:
            pass  # closed
        finally:
            self._finalized.set()

    def finish(self, wait: float = 3.0) -> str:
        """Key released: record the tail, send all of it, close, return the final text. Blocks for up to a few seconds."""
        if not self.active:
            return self.text
        time.sleep(self.TAIL)
        self.active = False
        self._wait = wait
        self.mic.stop()
        self._released.set()
        if self._worker:
            self._worker.join(wait + 12)  # connecting (8 s at most) + sending + the final text
        if self.interim:  # nothing final arrived for the tail: keep what was heard
            self.finals.append(self.interim)
            self.interim = ""
        return self.text

    def cancel(self) -> None:
        self.active = False
        self._cancelled = True
        self.mic.stop()
        self._released.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
