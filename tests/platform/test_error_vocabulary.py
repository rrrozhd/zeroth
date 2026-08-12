"""Backend failures are reported as a category, never as driver text.

A02-4/A02-8/A02-10: handlers interpolated the caught exception into the response
body, so a driver's host, port, DSN, or URL reached the caller -- on
``/health/ready``, an unauthenticated one.
"""

from __future__ import annotations

import pytest

from zeroth.platform.primitives import (
    ErrorCategory,
    categorize_exception,
    safe_error_detail,
)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ConnectionRefusedError("refused"), ErrorCategory.UNREACHABLE),
        (ConnectionError("no route"), ErrorCategory.UNREACHABLE),
        (TimeoutError("slow"), ErrorCategory.TIMEOUT),
        (PermissionError("denied"), ErrorCategory.AUTHENTICATION_FAILED),
        (FileNotFoundError("missing"), ErrorCategory.NOT_FOUND),
        (KeyError("missing"), ErrorCategory.NOT_FOUND),
        (ValueError("bad"), ErrorCategory.INVALID_CONFIGURATION),
        (TypeError("bad"), ErrorCategory.INVALID_CONFIGURATION),
        (ImportError("absent"), ErrorCategory.UNAVAILABLE),
        (ModuleNotFoundError("absent"), ErrorCategory.UNAVAILABLE),
    ],
)
def test_builtin_exceptions_resolve_to_their_category(
    exc: Exception, expected: ErrorCategory
) -> None:
    assert categorize_exception(exc) is expected


def test_an_unregistered_exception_falls_back_to_internal_error() -> None:
    """Fail-closed: forgetting to register a class loses precision, not secrecy."""

    class SomeUnregisteredDriverError(Exception):
        pass

    assert (
        categorize_exception(SomeUnregisteredDriverError("dsn=postgres://u:p@host/db"))
        is ErrorCategory.INTERNAL_ERROR
    )


def test_a_driver_subclass_resolves_through_its_registered_base() -> None:
    """MRO resolution is why every redis/httpx subclass need not be enumerated."""

    class RedisConnectionError(ConnectionError):
        pass

    assert categorize_exception(RedisConnectionError("...")) is ErrorCategory.UNREACHABLE


def test_a_class_named_like_a_driver_error_resolves_by_name() -> None:
    """Third-party classes that derive from plain Exception match by class name."""

    class OperationalError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    assert categorize_exception(OperationalError("...")) is ErrorCategory.UNREACHABLE
    assert (
        categorize_exception(AuthenticationError("..."))
        is ErrorCategory.AUTHENTICATION_FAILED
    )


@pytest.mark.parametrize(
    "secret_bearing_message",
    [
        "could not connect to 10.0.3.14:6379",
        "FATAL: password authentication failed for user 'zeroth'",
        'connection to server at "db.internal" (172.18.0.2), port 5432 failed',
        "dsn=postgresql://admin:hunter2@db.internal:5432/zeroth",
        "https://internal-regulus.svc.cluster.local:8443/v1/costs returned 500",
    ],
)
def test_no_part_of_the_exception_message_survives(secret_bearing_message: str) -> None:
    """The message is never read -- this is the whole property the finding names."""
    detail = safe_error_detail(RuntimeError(secret_bearing_message), context="database")

    assert secret_bearing_message not in detail
    for fragment in ("10.0.3.14", "hunter2", "db.internal", "5432", "cluster.local"):
        assert fragment not in detail


def test_detail_is_built_only_from_context_and_category() -> None:
    detail = safe_error_detail(ConnectionRefusedError("at 127.0.0.1:6379"), context="redis")

    assert detail == "redis: unreachable"


def test_every_category_value_is_a_bare_label() -> None:
    """A category is a term, not a sentence that could have carried a value."""
    for category in ErrorCategory:
        assert category.value.replace("_", "").isalpha()
