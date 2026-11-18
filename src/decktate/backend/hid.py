"""Real Stream Deck over USB HID (python-elgato-streamdeck + hidapi)."""

from __future__ import annotations

from PIL import Image

from .base import DeckBackend, KeyCallback


class DeckNotFound(RuntimeError):
    pass


class HidDeck(DeckBackend):
    def __init__(self, serial: str | None = None, brightness: int = 60, usb_id: str | None = None):
        """usb_id: "vvvv:pppp" (hex) to pick a deck by USB vendor and product id."""
        from StreamDeck.DeviceManager import DeviceManager
        from StreamDeck.ImageHelpers import PILHelper

        self._helper = PILHelper
        decks = [d for d in DeviceManager().enumerate() if d.is_visual()]
        if usb_id:
            try:
                vid, pid = (int(x, 16) for x in usb_id.lower().split(":"))
            except ValueError:
                raise DeckNotFound(f"bad USB id {usb_id!r}: expected vvvv:pppp") from None
            decks = [d for d in decks if (d.vendor_id(), d.product_id()) == (vid, pid)]
        if not decks:
            raise DeckNotFound("no Stream Deck found" + (f" with USB id {usb_id}" if usb_id else "")
                               + " (is the Elgato app or another program holding it?)")
        deck = None
        for d in decks:
            d.open()
            if serial is None or d.get_serial_number() == serial:
                deck = d
                break
            d.close()
        if deck is None:
            raise DeckNotFound(f"no Stream Deck with serial {serial}")
        self._deck = deck
        # Image format, size and flip are read from the device, never hardcoded.
        fmt = deck.key_image_format()
        self.rows, self.cols = deck.key_layout()
        self.key_size = tuple(fmt["size"])
        self._fmt = fmt
        deck.reset()
        deck.set_brightness(brightness)

    def info(self) -> dict:
        d = self._deck
        return {
            **super().info(),
            "type": d.deck_type(),
            "serial": d.get_serial_number(),
            "firmware": d.get_firmware_version(),
            "image_format": self._fmt,
        }

    def set_key_image(self, key: int, image: Image.Image) -> None:
        if image.size != self.key_size:
            image = image.resize(self.key_size, Image.LANCZOS)
        native = self._helper.to_native_key_format(self._deck, image.convert("RGB"))
        with self._deck:
            self._deck.set_key_image(key, native)

    def set_brightness(self, percent: int) -> None:
        with self._deck:
            self._deck.set_brightness(max(0, min(100, percent)))

    def alive(self) -> bool:
        try:
            return bool(self._deck.is_open() and self._deck.connected())
        except Exception:
            return False

    def on_key(self, callback: KeyCallback) -> None:
        self._deck.set_key_callback(lambda _deck, key, pressed: callback(key, pressed))

    def close(self) -> None:
        try:
            with self._deck:
                self._deck.reset()
                self._deck.close()
        except Exception:
            pass
