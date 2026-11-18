"""Deck backend interface. The UI only ever talks to this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from PIL import Image

KeyCallback = Callable[[int, bool], None]  # (key index, pressed)


class DeckBackend(ABC):
    """A grid of image keys. Keys are indexed row-major from the top left."""

    rows: int
    cols: int
    key_size: tuple[int, int]

    @property
    def key_count(self) -> int:
        return self.rows * self.cols

    @abstractmethod
    def set_key_image(self, key: int, image: Image.Image) -> None: ...

    @abstractmethod
    def set_brightness(self, percent: int) -> None: ...

    @abstractmethod
    def on_key(self, callback: KeyCallback) -> None:
        """Register the key callback. May be invoked from another thread."""

    @abstractmethod
    def close(self) -> None: ...

    def alive(self) -> bool:
        """Is the deck still there? Asked regularly, so that an unplugged deck is noticed even while nothing
        on it changes."""
        return True

    def info(self) -> dict:
        return {"rows": self.rows, "cols": self.cols, "key_size": self.key_size}
