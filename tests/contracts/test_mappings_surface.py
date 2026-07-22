"""Canonical import surface for the mappings contracts package.

Non-golden boundary tests for the Task 12 mappings move: the canonical
``zeroth.contracts.mappings`` package must publish the same objects the legacy
``zeroth.core.mappings`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_mappings_is_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts import mappings as canonical
    from zeroth.core import mappings as legacy

    assert canonical.ConstantMappingOperation is legacy.ConstantMappingOperation
    assert canonical.DefaultMappingOperation is legacy.DefaultMappingOperation
    assert canonical.EdgeMapping is legacy.EdgeMapping
    assert canonical.MappingExecutionError is legacy.MappingExecutionError
    assert canonical.MappingExecutor is legacy.MappingExecutor
    assert canonical.MappingOperation is legacy.MappingOperation
    assert canonical.MappingValidationError is legacy.MappingValidationError
    assert canonical.MappingValidator is legacy.MappingValidator
    assert canonical.PassthroughMappingOperation is legacy.PassthroughMappingOperation
    assert canonical.RenameMappingOperation is legacy.RenameMappingOperation
    assert canonical.TransformMappingOperation is legacy.TransformMappingOperation


def test_mapping_errors_and_models_are_the_same_through_both_paths() -> None:
    from zeroth.contracts.mappings import errors as canonical_errors
    from zeroth.contracts.mappings import models as canonical_models
    from zeroth.core.mappings import errors as legacy_errors
    from zeroth.core.mappings import models as legacy_models

    assert canonical_errors.MappingExecutionError is legacy_errors.MappingExecutionError
    assert canonical_errors.MappingValidationError is legacy_errors.MappingValidationError
    assert canonical_models.MappingOperationBase is legacy_models.MappingOperationBase
    assert canonical_models.EdgeMapping is legacy_models.EdgeMapping


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.contracts.mappings", "zeroth.core.mappings"),
        ("zeroth.core.mappings", "zeroth.contracts.mappings"),
    ],
)
def test_mappings_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
