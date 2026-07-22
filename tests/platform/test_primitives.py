"""Tests for shared backend platform primitives."""

from datetime import UTC, datetime
from uuid import UUID

from zeroth.platform.primitives import Clock, SystemClock, new_uuid, utc_now


def test_utc_now_returns_an_aware_utc_datetime() -> None:
    now = utc_now()

    assert now.tzinfo is UTC


def test_system_clock_implements_clock_protocol() -> None:
    clock: Clock = SystemClock()

    now = clock.now()

    assert isinstance(now, datetime)
    assert now.tzinfo is UTC


def test_clock_protocol_accepts_a_deterministic_structural_fake() -> None:
    fixed = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)

    class FakeClock:
        def now(self) -> datetime:
            return fixed

    clock: Clock = FakeClock()

    assert isinstance(clock, Clock)
    assert clock.now() == fixed


def test_new_uuid_returns_a_canonical_uuid_string() -> None:
    identifier = new_uuid()

    assert identifier == str(UUID(identifier))
