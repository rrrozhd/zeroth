"""Clock primitives for backend code."""

from datetime import UTC, datetime
from typing import Protocol


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


class Clock(Protocol):
    """Provide the current time."""

    def now(self) -> datetime:
        """Return the current time."""
        ...


class SystemClock:
    """Read the current time from the system clock."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        return utc_now()
