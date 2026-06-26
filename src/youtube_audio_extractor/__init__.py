from __future__ import annotations

__version__ = "0.3.0"

__all__ = ["main", "__version__"]


def main() -> None:
    from .app import main as desktop_main

    desktop_main()
