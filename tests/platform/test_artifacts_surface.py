"""Canonical import surface for the platform artifacts package.

Non-golden boundary tests for the Task 11 artifacts move: the canonical
``zeroth.platform.artifacts`` package must publish the same objects the legacy
``zeroth.core.artifacts`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_artifacts_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import artifacts as legacy
    from zeroth.platform import artifacts as canonical

    assert canonical.ArtifactNotFoundError is legacy.ArtifactNotFoundError
    assert canonical.ArtifactReference is legacy.ArtifactReference
    assert canonical.ArtifactStorageError is legacy.ArtifactStorageError
    assert canonical.ArtifactStore is legacy.ArtifactStore
    assert canonical.ArtifactStoreError is legacy.ArtifactStoreError
    assert canonical.ArtifactStoreSettings is legacy.ArtifactStoreSettings
    assert canonical.ArtifactTTLError is legacy.ArtifactTTLError
    assert canonical.FilesystemArtifactStore is legacy.FilesystemArtifactStore
    assert canonical.RedisArtifactStore is legacy.RedisArtifactStore
    assert canonical.generate_artifact_key is legacy.generate_artifact_key


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.platform.artifacts", "zeroth.core.artifacts"),
        ("zeroth.core.artifacts", "zeroth.platform.artifacts"),
    ],
)
def test_artifacts_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
