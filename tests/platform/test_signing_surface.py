"""Canonical import surface for the platform signing package.

Non-golden boundary tests for the Task 11 signing move: the canonical
``zeroth.platform.signing`` package must publish the same objects the legacy
``zeroth.core.signing`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_signing_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import signing as legacy
    from zeroth.platform import signing as canonical

    assert canonical.Ed25519Signer is legacy.Ed25519Signer
    assert canonical.EnvHmacSigner is legacy.EnvHmacSigner
    assert canonical.NullSigner is legacy.NullSigner
    assert canonical.SigningConfigError is legacy.SigningConfigError
    assert canonical.SigningKeyProvider is legacy.SigningKeyProvider
    assert canonical.build_signing_provider is legacy.build_signing_provider
    assert canonical.build_signing_provider_async is legacy.build_signing_provider_async
    assert canonical.sign_digest is legacy.sign_digest
    assert canonical.signable_bytes is legacy.signable_bytes
    assert canonical.verify_digest is legacy.verify_digest


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.platform.signing", "zeroth.core.signing"),
        ("zeroth.core.signing", "zeroth.platform.signing"),
    ],
)
def test_signing_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
