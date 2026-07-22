"""Shared backend primitives."""

from zeroth.platform.primitives.clock import Clock, SystemClock, utc_now
from zeroth.platform.primitives.identifiers import new_uuid

__all__ = ["Clock", "SystemClock", "new_uuid", "utc_now"]
