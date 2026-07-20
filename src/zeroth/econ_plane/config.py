"""Legacy import path for :mod:`zeroth.econ.plane.config`."""

from __future__ import annotations

import zeroth.econ.plane.config as _config


def __getattr__(name: str) -> object:
    return getattr(_config, name)
