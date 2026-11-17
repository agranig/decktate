"""Decktate: a Stream Deck control surface for Claude Code."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as run

    run()
