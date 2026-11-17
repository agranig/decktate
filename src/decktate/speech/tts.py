"""Text to speech. ElevenLabs when a key is configured, otherwise the
Mac's own voice (`say`), so spoken prompts work out of the box. Short fixed phrases are cached on disk.
"""

from __future__ import annotations

import hashlib
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

import httpx

from ..config import Config
from ..core.redact import pieces, redact
from .audio import Player, chime

API = "https://api.elevenlabs.io"
RATE = 24000
EL_SPEED = (0.7, 1.2)  # the range ElevenLabs accepts for voice_settings.speed


def say_rate(speed: float) -> int:
    """macOS `say -r` value for a speed factor. The option does not scale linearly
    with speaking time, so this follows a measured fit (1.0 = the voice's normal pace)."""
    return int(round((min(2.0, max(0.5, speed)) - 0.27) / 0.00455))


class TTSError(RuntimeError):
    pass


class ElevenLabs:
    name = "elevenlabs"

    def __init__(self, key: str, cfg: Config, client: httpx.Client | None = None):
        self.cfg = cfg
        self._http = client or httpx.Client(base_url=API, headers={"xi-api-key": key}, timeout=30)
        self._voice = cfg.speech.voice_id

    def check(self) -> str:
        """'' if the key works, else why not. 403 = valid key without permission for this call."""
        r = self._http.get("/v1/user")
        if r.status_code == 401:
            return "invalid API key"
        return "" if r.status_code in (200, 403) else f"HTTP {r.status_code}"

    def voices(self) -> list[dict]:
        r = self._http.get("/v2/voices", params={"page_size": 100, "include_total_count": "false"})
        r.raise_for_status()
        return [{"id": v["voice_id"], "name": v.get("name", ""), "category": v.get("category", "")}
                for v in r.json().get("voices", [])]

    def voice(self) -> str:
        """The configured voice, or the account's first one (premade voice ids are not stable across accounts)."""
        if not self._voice:
            found = self.voices()
            if not found:
                raise TTSError("the ElevenLabs account has no voices")
            self._voice = found[0]["id"]
        return self._voice

    def stream(self, text: str):
        """Yield raw 16-bit 24 kHz mono PCM chunks."""
        lang = self.cfg.speech.language
        speed = min(EL_SPEED[1], max(EL_SPEED[0], float(self.cfg.speech.speed)))
        body = {"text": text, "model_id": self.cfg.speech.tts_model, "apply_text_normalization": "on",
                "voice_settings": {"speed": speed}}
        if lang in ("en", "de"):
            body["language_code"] = lang
        with self._http.stream("POST", f"/v1/text-to-speech/{self.voice()}/stream",
                               params={"output_format": f"pcm_{RATE}"}, json=body) as r:
            if r.status_code == 404:
                self._voice = ""  # the voice is gone (default voices are being retired): pick again next time
            if r.status_code >= 400:
                r.read()
                try:
                    detail = r.json().get("detail") or {}
                    code = detail.get("code") or detail.get("status") or r.status_code
                except Exception:
                    code = r.status_code
                raise TTSError(f"ElevenLabs: {code}")
            yield from r.iter_bytes(4096)


class SystemVoice:
    """The OS speech synthesizer, rendered to PCM so it plays on the chosen speaker and can be cut off."""

    name = "system"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    @staticmethod
    def available() -> bool:
        return bool(shutil.which("say"))

    def stream(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "speech.wav")
            lang = self.cfg.speech.language
            speed = float(self.cfg.speech.speed)
            cmd = ["say", "-r", str(say_rate(speed)), "-o", out, f"--data-format=LEI16@{RATE}"]
            if lang == "de":
                cmd += ["-v", "Anna"]
            cmd.append(text)
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=30)
                with wave.open(out, "rb") as w:
                    pcm = w.readframes(w.getnframes())
            except Exception as exc:
                raise TTSError(f"system voice: {type(exc).__name__}") from None
        yield pcm


class Speaker:
    """Speaks text on a background thread. speak() replaces what is being said; stop() silences it."""

    def __init__(self, cfg: Config, player: Player | None = None, engine=None):
        self.cfg = cfg
        self.player = player or Player(cfg.audio.output, cfg.audio.volume)
        self.engine = engine if engine is not None else self._pick_engine()
        self.error = ""
        self.last_text = ""
        self._q: queue.Queue = queue.Queue()
        self._cache = cfg.home / "tts-cache"
        self._generation = 0
        self._talking = False  # in the middle of a text (also between two pieces of it)
        threading.Thread(target=self._run, daemon=True, name="speaker").start()

    def _pick_engine(self):
        mode, key = self.cfg.speech.tts, os.environ.get("ELEVENLABS_API_KEY", "")
        if mode == "off":
            return None
        if mode in ("auto", "elevenlabs") and key:
            return ElevenLabs(key, self.cfg)
        if mode in ("auto", "system") and SystemVoice.available():
            return SystemVoice(self.cfg)
        return None

    @property
    def enabled(self) -> bool:
        return self.engine is not None

    @property
    def speaking(self) -> bool:
        return self.player.playing or self._talking or not self._q.empty()

    def stop(self) -> None:
        self._generation += 1
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self.player.stop()

    def chime(self, kind: str) -> None:
        self._q.put(("chime", kind, self._generation))

    def speak(self, text: str, cache: bool = False, remember: bool = True) -> None:
        """Say it now, cutting off anything in progress. Secrets are redacted first."""
        text = redact(text or "").strip()
        if not text:
            return
        if remember:
            self.last_text = text
        if not self.enabled:
            return
        self.stop()
        self._q.put(("speak", (text, cache), self._generation))

    def repeat(self) -> None:
        if self.last_text:
            self.speak(self.last_text)

    def _cache_file(self, text: str) -> Path:
        sp = self.cfg.speech
        tag = f"{self.engine.name}|{sp.voice_id}|{sp.tts_model}|{sp.language}|{sp.speed}|{text}"
        return self._cache / (hashlib.sha256(tag.encode()).hexdigest()[:24] + ".pcm")

    def _run(self) -> None:
        while True:
            kind, arg, generation = self._q.get()
            if generation != self._generation:
                continue  # superseded while waiting
            try:
                if kind == "chime":
                    self.player.play(chime(arg), RATE)
                    continue
                text, cache = arg
                path = self._cache_file(text)
                if cache and path.exists():
                    self.player.play(path.read_bytes(), RATE)
                    continue
                self._talking = True
                # a long text is spoken piece by piece: the voice starts at once, and "quiet" ends it between pieces too
                for part in pieces(text):
                    if generation != self._generation:
                        break
                    collected: list[bytes] = []

                    def tee(part=part, collected=collected):
                        for chunk in self.engine.stream(part):
                            collected.append(chunk)
                            yield chunk

                    finished = self.player.play(tee(), RATE)
                    self.error = self.player.error
                    if not finished:
                        break
                    if cache and collected and part == text:
                        self._cache.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"".join(collected))
            except TTSError as exc:
                self.error = str(exc)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            finally:
                self._talking = False
