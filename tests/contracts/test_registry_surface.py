"""Canonical import surface for the contract registry package.

Non-golden boundary tests for the Task 12 registry move: the canonical
``zeroth.contracts.registry`` package must publish the same objects the legacy
``zeroth.core.contracts`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.

The third cold-import pairing pins the deliberate cycle-free seam with the
vendored governed bundle: ``zeroth.core.governed.tools.base`` imports
``ExecutionPlacement`` from ``zeroth.contracts.registry.tooling`` while
``zeroth.contracts.registry.registry`` imports ``GovernedStepSpec`` from
``zeroth.core.governed.app.spec``. Both orders must initialize cleanly.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_registry_is_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts import registry as canonical
    from zeroth.core import contracts as legacy

    assert canonical.ContractNotFoundError is legacy.ContractNotFoundError
    assert canonical.ContractReference is legacy.ContractReference
    assert canonical.ContractRegistry is legacy.ContractRegistry
    assert canonical.ContractRegistryError is legacy.ContractRegistryError
    assert canonical.ContractVersion is legacy.ContractVersion
    assert canonical.StepContractBinding is legacy.StepContractBinding
    assert canonical.ToolContractBinding is legacy.ToolContractBinding
    assert canonical.validate_artifact_reference is legacy.validate_artifact_reference


def test_registry_errors_are_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts.registry import errors as canonical
    from zeroth.core.contracts import errors as legacy

    assert canonical.ContractNotFoundError is legacy.ContractNotFoundError
    assert canonical.ContractRegistryError is legacy.ContractRegistryError
    assert canonical.ContractTypeResolutionError is legacy.ContractTypeResolutionError
    assert canonical.ContractVersionExistsError is legacy.ContractVersionExistsError


def test_execution_placement_has_one_contract_owned_definition() -> None:
    from zeroth.contracts.registry import tooling
    from zeroth.core.governed.tools import base

    assert base.ExecutionPlacement is tooling.ExecutionPlacement


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.contracts.registry", "zeroth.core.contracts"),
        ("zeroth.core.contracts", "zeroth.contracts.registry"),
        ("zeroth.contracts.registry", "zeroth.core.governed"),
        ("zeroth.core.governed", "zeroth.contracts.registry"),
    ],
)
def test_registry_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
