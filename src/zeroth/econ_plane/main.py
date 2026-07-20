"""Legacy import path for :mod:`zeroth.econ.plane.main`."""

from __future__ import annotations

import zeroth.econ.plane.main as _main


def __getattr__(name: str) -> object:
    return getattr(_main, name)
