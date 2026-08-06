"""Canonical import surface for the platform signing package.

Non-golden boundary tests for the Task 11 signing move: the canonical
``zeroth.platform.signing`` package must publish the same objects the legacy
``zeroth.core.signing`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys


def test_signing_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.platform.signing as canonical

    expected = {
        "Ed25519Signer",
        "EnvHmacSigner",
        "NullSigner",
        "SigningConfigError",
        "SigningKeyProvider",
        "build_signing_provider",
        "build_signing_provider_async",
        "sign_digest",
        "signable_bytes",
        "verify_digest",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.platform.signing no longer publishes: {missing}"


def test_signing_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.platform.signing"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
