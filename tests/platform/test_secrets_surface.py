"""Canonical import surface for the platform secrets package.

Non-golden boundary tests for the Task 11 secrets move: the canonical
``zeroth.platform.secrets`` package must publish the same objects the legacy
``zeroth.core.secrets`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_secrets_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import secrets as legacy
    from zeroth.platform import secrets as canonical

    assert canonical.EnvSecretProvider is legacy.EnvSecretProvider
    assert canonical.SecretProvider is legacy.SecretProvider
    assert canonical.SecretProviderConfigError is legacy.SecretProviderConfigError
    assert canonical.SecretRedactor is legacy.SecretRedactor
    assert canonical.SecretResolutionError is legacy.SecretResolutionError
    assert canonical.SecretResolver is legacy.SecretResolver
    assert canonical.VaultSecretProvider is legacy.VaultSecretProvider
    assert canonical.build_secret_provider is legacy.build_secret_provider
    assert canonical.normalize_secret_name is legacy.normalize_secret_name
    assert canonical.resolve_async is legacy.resolve_async
    assert canonical.resolve_many_async is legacy.resolve_many_async
    assert canonical.resolve_secret_async is legacy.resolve_secret_async


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.platform.secrets", "zeroth.core.secrets"),
        ("zeroth.core.secrets", "zeroth.platform.secrets"),
    ],
)
def test_secrets_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
