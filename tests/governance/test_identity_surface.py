"""Canonical import surface for the governance identity package.

Non-golden boundary tests for the Task 13 identity move: the canonical
``zeroth.governance.identity`` package must publish the same objects the
legacy ``zeroth.core.identity`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_identity_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import identity as legacy
    from zeroth.governance import identity as canonical

    assert canonical.ActorIdentity is legacy.ActorIdentity
    assert canonical.AuthenticatedPrincipal is legacy.AuthenticatedPrincipal
    assert canonical.AuthMethod is legacy.AuthMethod
    assert canonical.PrincipalScope is legacy.PrincipalScope
    assert canonical.ServiceRole is legacy.ServiceRole


def test_identity_models_are_the_same_surface_through_both_paths() -> None:
    from zeroth.core.identity import models as legacy_models
    from zeroth.governance.identity import models as canonical_models

    assert canonical_models.ActorIdentity is legacy_models.ActorIdentity
    assert canonical_models.AuthenticatedPrincipal is legacy_models.AuthenticatedPrincipal
    assert canonical_models.AuthMethod is legacy_models.AuthMethod
    assert canonical_models.PrincipalScope is legacy_models.PrincipalScope
    assert canonical_models.ServiceRole is legacy_models.ServiceRole


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.governance.identity", "zeroth.core.identity"),
        ("zeroth.core.identity", "zeroth.governance.identity"),
    ],
)
def test_identity_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
