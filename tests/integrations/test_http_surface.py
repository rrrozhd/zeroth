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


def test_http_publishes_its_whole_surface() -> None:
    from zeroth.integrations import http as canonical

    for name in EXPORTS:
        assert hasattr(canonical, name), name


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
def test_http_modules_publish_their_names(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    canonical_module = importlib.import_module(f"zeroth.integrations.http.{module_name}")

    for name in names:
        assert hasattr(canonical_module, name), name


def test_http_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.integrations.http"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
