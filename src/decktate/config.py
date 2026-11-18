"""Configuration: ~/.config/decktate/config.yaml, keys from the environment
or ~/.config/decktate/.env, and the shared hook token."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

HOME = Path(os.environ.get("DECKTATE_HOME", "~/.config/decktate")).expanduser()


@dataclass
class Speech:
    tts: str = "auto"  # auto | elevenlabs | system | off   (auto: ElevenLabs if a key is set, else the OS voice)
    voice_id: str = ""  # ElevenLabs voice; empty = first voice of the account
    tts_model: str = "eleven_flash_v2_5"
    stt_model: str = "nova-3"
    language: str = "en"  # en | de | multi
    speak_permissions: bool = True
    speak_on_stop: bool = True
    review_dictation: bool = False  # True: show the dictated text first and send it with a second tap
    speed: float = 1.2  # talking speed; 1.0 is normal. ElevenLabs accepts 0.7 to 1.2, the system voice up to 2.0
    keyterms: list[str] = field(default_factory=lambda: ["Claude", "refactor", "commit", "pull request", "pytest"])


@dataclass
class Audio:
    input: str = ""  # device name (substring) or index; empty = system default
    output: str = ""
    volume: float = 1.0


@dataclass
class Config:
    port: int = 47873  # hooks are served on 127.0.0.1:<port>; fixed, because the installed hooks point at it
    brightness: int = 60
    hold_ms: int = 600
    confirm_ms: int = 1200
    permission_hold_s: float = 120  # how long a permission request waits for a key before the terminal takes over
    log_level: str = "info"  # activity.log: info = what was done; debug = also every key, hook event and screen reading
    auto_hooks: bool = True  # add the hooks to Claude Code's settings on start and remove them on shutdown
    slash: list[str] = field(default_factory=lambda: ["/btw", "/compact", "/clear", "/review", "/context", "/usage", "/resume"])
    mute_projects: list[str] = field(default_factory=list)  # project paths that are never spoken
    speech: Speech = field(default_factory=Speech)
    audio: Audio = field(default_factory=Audio)
    home: Path = HOME

    @property
    def audit_log(self) -> Path:
        return self.home / "audit.jsonl"

    def token(self) -> str:
        """Shared secret the hooks send. Created on first use, readable by the user only."""
        path = self.home / "token"
        if not path.exists():
            self.home.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_urlsafe(24))
            path.chmod(0o600)
        return path.read_text().strip()

    def save(self) -> None:
        """Persist the settings that can be changed from the deck."""
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / "config.yaml"
        raw = yaml.safe_load(path.read_text()) if path.exists() else {}
        raw = raw or {}
        raw.setdefault("speech", {}).update(voice_id=self.speech.voice_id, language=self.speech.language, tts=self.speech.tts,
                                            speed=self.speech.speed, speak_permissions=self.speech.speak_permissions,
                                            speak_on_stop=self.speech.speak_on_stop)
        raw.setdefault("audio", {}).update(input=self.audio.input, output=self.audio.output, volume=self.audio.volume)
        raw["brightness"] = self.brightness
        raw["log_level"] = self.log_level
        path.write_text(yaml.safe_dump(raw, sort_keys=False))


def _only(cls, raw) -> dict:
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in (raw or {}).items() if k in known}


def load_env(home: Path = HOME) -> list[str]:
    """KEY=VALUE lines from <home>/.env; the real environment wins."""
    names = []
    path = home / ".env"
    if not path.exists():
        return names
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in ("'", '"') and value.find(value[0], 1) > 0:
            value = value[1:value.find(value[0], 1)]  # "value" # a note: what is inside the quotes, whatever follows
        else:
            value = value.replace("\t#", " #").split(" #", 1)[0].strip()  # value   # a note
        os.environ.setdefault(key.strip(), value)
        names.append(key.strip())
    return names


def load(home: Path | None = None) -> Config:
    home = Path(home) if home else HOME
    path = home / "config.yaml"
    raw = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    top = _only(Config, raw)
    top.pop("speech", None), top.pop("audio", None), top.pop("home", None)
    return Config(**top, speech=Speech(**_only(Speech, raw.get("speech"))),
                  audio=Audio(**_only(Audio, raw.get("audio"))), home=home)
