"""Canonical import surface for the governance identity package.

Non-golden boundary tests for the Task 13 identity move: the canonical
``zeroth.governance.identity`` package must publish the same objects the
legacy ``zeroth.core.identity`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys


def test_identity_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.governance.identity as canonical

    expected = {
        "ActorIdentity",
        "AuthMethod",
        "AuthenticatedPrincipal",
        "PrincipalScope",
        "ServiceRole",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.governance.identity no longer publishes: {missing}"


def test_identity_models_publish_their_names() -> None:
    from zeroth.governance.identity import models as canonical_models

    assert hasattr(canonical_models, "ActorIdentity")
    assert hasattr(canonical_models, "AuthenticatedPrincipal")
    assert hasattr(canonical_models, "AuthMethod")
    assert hasattr(canonical_models, "PrincipalScope")
    assert hasattr(canonical_models, "ServiceRole")


def test_identity_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.governance.identity"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
