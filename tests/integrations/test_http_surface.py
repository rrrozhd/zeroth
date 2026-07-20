"""Canonical import surface for the http integrations package.

Non-golden boundary tests for the Task 15 http move: the canonical
``zeroth.integrations.http`` package must publish the same objects the
legacy ``zeroth.core.http`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

EXPORTS = (
    "AuthType",
    "CircuitBreaker",
    "CircuitBreakerRegistry",
    "CircuitOpenError",
    "CircuitState",
    "EndpointConfig",
    "HttpCallRecord",
    "HttpClientError",
    "HttpClientSettings",
    "HttpRateLimitError",
    "HttpRetryExhaustedError",
    "InMemoryTokenBucket",
    "ResilientHttpClient",
    "redact_url",
)


def test_http_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import http as legacy
    from zeroth.integrations import http as canonical

    for name in EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        (
            "circuit_breaker",
            (
                "CircuitBreaker",
                "CircuitBreakerRegistry",
                "CircuitState",
                "InMemoryTokenBucket",
            ),
        ),
        ("client", ("ResilientHttpClient",)),
        (
            "errors",
            (
                "CircuitOpenError",
                "HttpClientError",
                "HttpRateLimitError",
                "HttpRetryExhaustedError",
            ),
        ),
        (
            "models",
            (
                "AuthType",
                "EndpointConfig",
                "HttpCallRecord",
                "HttpClientSettings",
                "redact_url",
            ),
        ),
    ],
)
def test_http_modules_are_the_same_surface_through_both_paths(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    legacy_module = importlib.import_module(f"zeroth.core.http.{module_name}")
    canonical_module = importlib.import_module(f"zeroth.integrations.http.{module_name}")

    for name in names:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.integrations.http", "zeroth.core.http"),
        ("zeroth.core.http", "zeroth.integrations.http"),
    ],
)
def test_http_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
