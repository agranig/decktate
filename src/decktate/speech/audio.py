"""Microphone and speaker handling (PortAudio via sounddevice).

Devices are detected at runtime and chosen by name, so a headset that is
plugged in later is picked up, and a configured device that is gone falls
back to the system default instead of failing.
"""

from __future__ import annotations

import math
import queue
import threading
from dataclasses import dataclass

import numpy as np

MIC_RATE = 16000  # what the speech-to-text service gets
BLOCK = 1600  # 100 ms of microphone audio per chunk


@dataclass
class Device:
    index: int
    name: str
    inputs: int
    outputs: int
    default_in: bool = False
    default_out: bool = False


def _sd():
    import sounddevice as sd

    return sd


def devices(refresh: bool = False) -> list[Device]:
    sd = _sd()
    if refresh:  # PortAudio only sees newly plugged devices after a re-init
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass
    try:
        din, dout = sd.default.device
        return [Device(i, d["name"], d["max_input_channels"], d["max_output_channels"], i == din, i == dout)
                for i, d in enumerate(sd.query_devices())]
    except Exception:
        return []


def microphones() -> list[Device]:
    return [d for d in devices() if d.inputs > 0]


def speakers() -> list[Device]:
    return [d for d in devices() if d.outputs > 0]


def pick(wanted: str, kind: str) -> Device | None:
    """The configured device (name substring or index), else the system default, else the first."""
    pool = microphones() if kind == "input" else speakers()
    if not pool:
        return None
    if wanted:
        for d in pool:
            if wanted.isdigit() and d.index == int(wanted) or wanted.lower() in d.name.lower():
                return d
    default = [d for d in pool if (d.default_in if kind == "input" else d.default_out)]
    return (default or pool)[0]


def chime(kind: str = "ok", rate: int = 24000) -> np.ndarray:
    """Short synthesized tones: ok (rising), no (falling), alert (double), tick."""
    notes = {"ok": [(660, 0.07), (990, 0.11)], "no": [(520, 0.08), (330, 0.14)],
             "alert": [(880, 0.07), (0, 0.05), (880, 0.07), (0, 0.05), (1175, 0.12)], "tick": [(1200, 0.03)],
             "start": [(520, 0.05), (780, 0.06)], "stop": [(780, 0.05), (520, 0.06)]}[kind]
    out = []
    for freq, secs in notes:
        n = int(rate * secs)
        t = np.arange(n) / rate
        tone = np.sin(2 * math.pi * freq * t) if freq else np.zeros(n)
        fade = np.minimum(1.0, np.minimum(np.arange(n), n - np.arange(n)) / (rate * 0.008))
        out.append(tone * fade * 0.28)
    return (np.concatenate(out) * 32767).astype(np.int16)


class Player:
    """Plays 16-bit mono PCM on the chosen speaker. One thing at a time; stop() cuts it off."""

    def __init__(self, device: str = "", volume: float = 1.0):
        self.device, self.volume = device, volume
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.playing = False
        self.error = ""

    def stop(self) -> None:
        self._stop.set()

    def play(self, chunks, rate: int = 24000) -> bool:
        """chunks: an int16 array, raw bytes, or an iterable of byte chunks (streamed). Blocks until done."""
        sd = _sd()
        dev = pick(self.device, "output")
        if dev is None:
            self.error = "no speaker"
            return False
        if isinstance(chunks, np.ndarray):
            chunks = [chunks.tobytes()]
        elif isinstance(chunks, (bytes, bytearray)):
            chunks = [bytes(chunks)]
        with self._lock:
            self._stop.clear()
            self.playing, self.error = True, ""
            try:
                with sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16", device=dev.index) as stream:
                    carry = b""
                    for chunk in chunks:
                        if self._stop.is_set():
                            break
                        data = carry + chunk
                        carry = data[len(data) - (len(data) % 2):]
                        data = data[: len(data) - len(carry)]
                        if self.volume != 1.0 and data:
                            data = (np.frombuffer(data, dtype=np.int16) * self.volume).clip(-32768, 32767).astype(np.int16).tobytes()
                        for i in range(0, len(data), 4800):  # small writes so stop() takes effect quickly
                            if self._stop.is_set():
                                break
                            stream.write(data[i:i + 4800])
                    if self._stop.is_set():
                        stream.abort()
                return not self._stop.is_set()
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return False
            finally:
                self.playing = False


class Microphone:
    """Captures 16 kHz mono PCM chunks from the chosen microphone and tracks the input level."""

    def __init__(self, device: str = ""):
        self.device = device
        self.level = 0.0  # 0..1, smoothed, for the meter on the key
        self.frames = 0  # chunks captured since start(): above zero means the microphone really delivers sound
        self.error = ""
        self._q: queue.Queue = queue.Queue()
        self._stream = None

    def start(self) -> bool:
        sd = _sd()
        dev = pick(self.device, "input")
        if dev is None:
            self.error = "no microphone"
            return False
        self._q = queue.Queue()
        self.level, self.error, self.frames = 0.0, "", 0

        def callback(indata, frames, time_info, status):
            pcm = bytes(indata)
            samples = np.frombuffer(pcm, dtype=np.int16)
            rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768 if len(samples) else 0.0
            self.level = max(min(1.0, rms * 9), self.level * 0.7)
            self.frames += 1
            self._q.put(pcm)

        try:
            self._stream = sd.RawInputStream(samplerate=MIC_RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                             device=dev.index, callback=callback)
            self._stream.start()
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._stream = None
            return False

    def read(self, timeout: float = 0.2) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self.level = 0.0
