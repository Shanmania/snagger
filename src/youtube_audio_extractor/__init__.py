from __future__ import annotations

__all__ = ["main"]


def main() -> None:
    from .app import main as desktop_main

    desktop_main()
