"""Static assets for the Zeroth Console.

This package carries the built Next.js static export of the console UI so a
plain ``pip install "zeroth-core[console]"`` can serve the console at
``/console`` without a Node toolchain or a source checkout. zeroth-core
discovers it via :func:`console_dir` (see ``zeroth.core.service.console_ui``).
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

__all__ = ["console_dir"]


def console_dir() -> Path:
    """Path to the bundled console static export (the dir with index.html)."""
    return Path(str(files("zeroth_console").joinpath("static")))
