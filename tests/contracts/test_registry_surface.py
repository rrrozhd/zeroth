"""Canonical import surface for the contract registry package.

Non-golden boundary tests for the Task 12 registry move: the canonical
``zeroth.contracts.registry`` package must publish the same objects the legacy
``zeroth.core.contracts`` path keeps republishing, and both packages must stay
cold-importable from a fresh interpreter in either order.

The third cold-import pairing pins the deliberate cycle-free seam with the
vendored governed bundle: ``zeroth.core.governed.tools.base`` imports
``ExecutionPlacement`` from ``zeroth.contracts.registry.tooling``, so
importing the legacy governed aggregator pulls in the canonical registry
package mid-initialization. Both orders must initialize cleanly.
"""

from __future__ import annotations

import subprocess
import sys


def test_errors_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.contracts.registry as canonical

    expected = {
        "ContractNotFoundError",
        "ContractReference",
        "ContractRegistry",
        "ContractRegistryError",
        "ContractVersion",
        "StepContractBinding",
        "ToolContractBinding",
        "validate_artifact_reference",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.contracts.registry no longer publishes: {missing}"


def test_registry_errors_publish_their_names() -> None:
    from zeroth.contracts.registry import errors as canonical

    assert hasattr(canonical, "ContractNotFoundError")
    assert hasattr(canonical, "ContractRegistryError")
    assert hasattr(canonical, "ContractTypeResolutionError")
    assert hasattr(canonical, "ContractVersionExistsError")


def test_execution_placement_has_one_contract_owned_definition() -> None:
    from zeroth.contracts.registry import tooling
    from zeroth.runtime.agents.tooling import base

    assert base.ExecutionPlacement is tooling.ExecutionPlacement


def test_errors_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.contracts.registry.errors"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
