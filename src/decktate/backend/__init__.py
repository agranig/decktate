from .base import DeckBackend
from .sim import SimDeck

__all__ = ["DeckBackend", "SimDeck", "open_deck"]


def open_deck(prefer_hid: bool = True, **kw) -> DeckBackend:
    """Return the real deck if present, else a SimDeck."""
    if prefer_hid:
        try:
            from .hid import HidDeck

            return HidDeck(**kw)
        except Exception:
            pass
    return SimDeck()
