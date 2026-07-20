"""Legacy import path for :mod:`zeroth.econ.plane.database`."""

from __future__ import annotations

import zeroth.econ.plane.database as _database


def __getattr__(name: str) -> object:
    return getattr(_database, name)
