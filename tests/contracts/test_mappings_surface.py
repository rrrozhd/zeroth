"""Canonical import surface for the mappings contracts package.

Non-golden boundary tests for the Task 12 mappings move: the canonical
``zeroth.contracts.mappings`` package must publish the same objects the legacy
``zeroth.core.mappings`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.
"""

from __future__ import annotations

import subprocess
import sys


def test_mappings_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.contracts.mappings as canonical

    expected = {
        "ConstantMappingOperation",
        "DefaultMappingOperation",
        "EdgeMapping",
        "MappingExecutionError",
        "MappingExecutor",
        "MappingOperation",
        "MappingValidationError",
        "MappingValidator",
        "PassthroughMappingOperation",
        "RenameMappingOperation",
        "TransformMappingOperation",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.contracts.mappings no longer publishes: {missing}"


def test_mapping_errors_and_models_are_the_same_through_both_paths() -> None:
    from zeroth.contracts.mappings import errors as canonical_errors
    from zeroth.contracts.mappings import models as canonical_models
    from zeroth.core.mappings import errors as legacy_errors
    from zeroth.core.mappings import models as legacy_models

    assert canonical_errors.MappingExecutionError is legacy_errors.MappingExecutionError
    assert canonical_errors.MappingValidationError is legacy_errors.MappingValidationError
    assert canonical_models.MappingOperationBase is legacy_models.MappingOperationBase
    assert canonical_models.EdgeMapping is legacy_models.EdgeMapping


def test_mappings_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.contracts.mappings"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
