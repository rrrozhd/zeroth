"""Canonical import surface for the platform secrets package.

Non-golden boundary tests for the Task 11 secrets move: the canonical
``zeroth.platform.secrets`` package must publish the same objects the legacy
``zeroth.core.secrets`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys


def test_secrets_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.platform.secrets as canonical

    expected = {
        "EnvSecretProvider",
        "SecretProvider",
        "SecretProviderConfigError",
        "SecretRedactor",
        "SecretResolutionError",
        "SecretResolver",
        "VaultSecretProvider",
        "build_secret_provider",
        "normalize_secret_name",
        "resolve_async",
        "resolve_many_async",
        "resolve_secret_async",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.platform.secrets no longer publishes: {missing}"


def test_secrets_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.platform.secrets"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
