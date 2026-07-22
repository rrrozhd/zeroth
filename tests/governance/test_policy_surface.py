"""Canonical import surface for the governance policy package.

Non-golden boundary tests for the Task 13 policy move: the canonical
``zeroth.governance.policy`` package must publish the same objects the
legacy ``zeroth.core.policy`` path keeps republishing, and both packages
must stay cold-importable from a fresh interpreter in either order.

Two seams are pinned alongside the move. The ``Capability`` enum is defined
in ``zeroth.contracts.graph.models`` — it is authored graph vocabulary,
composed by ``AgentToolBinding.required_capabilities`` — while the policy
packages republish it in the legal governance-to-contracts direction.
``PolicyGuard.evaluate`` no longer names the runtime ``Run`` type, so the
policy domain stays off the run domain's import path entirely.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_policy_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import policy as legacy
    from zeroth.governance import policy as canonical

    assert canonical.Capability is legacy.Capability
    assert canonical.CapabilityDeniedError is legacy.CapabilityDeniedError
    assert canonical.CapabilityRegistry is legacy.CapabilityRegistry
    assert canonical.EnforcementResult is legacy.EnforcementResult
    assert canonical.PolicyDecision is legacy.PolicyDecision
    assert canonical.PolicyDefinition is legacy.PolicyDefinition
    assert canonical.PolicyGuard is legacy.PolicyGuard
    assert canonical.PolicyRegistry is legacy.PolicyRegistry
    assert canonical.apply_secret_policy is legacy.apply_secret_policy
    assert canonical.default_capability_registry is legacy.default_capability_registry
    assert canonical.parse_effective_capabilities is legacy.parse_effective_capabilities
    assert canonical.require_capabilities is legacy.require_capabilities


def test_policy_submodules_are_the_same_surface_through_both_paths() -> None:
    from zeroth.core.policy import errors as legacy_errors
    from zeroth.core.policy import guard as legacy_guard
    from zeroth.core.policy import models as legacy_models
    from zeroth.core.policy import registry as legacy_registry
    from zeroth.governance.policy import errors as canonical_errors
    from zeroth.governance.policy import guard as canonical_guard
    from zeroth.governance.policy import models as canonical_models
    from zeroth.governance.policy import registry as canonical_registry

    assert canonical_models.Capability is legacy_models.Capability
    assert canonical_models.EnforcementResult is legacy_models.EnforcementResult
    assert canonical_models.PolicyDecision is legacy_models.PolicyDecision
    assert canonical_models.PolicyDefinition is legacy_models.PolicyDefinition
    assert canonical_errors.CapabilityDeniedError is legacy_errors.CapabilityDeniedError
    assert canonical_guard.PolicyGuard is legacy_guard.PolicyGuard
    assert canonical_guard.apply_secret_policy is legacy_guard.apply_secret_policy
    assert canonical_registry.CapabilityRegistry is legacy_registry.CapabilityRegistry
    assert canonical_registry.PolicyRegistry is legacy_registry.PolicyRegistry


def test_capability_is_defined_in_the_graph_contracts() -> None:
    from zeroth.contracts import graph as graph_package
    from zeroth.contracts.graph import models as graph_models
    from zeroth.core.policy import models as legacy_models
    from zeroth.governance.policy import models as canonical_models

    assert canonical_models.Capability is graph_models.Capability
    assert legacy_models.Capability is graph_models.Capability
    assert graph_package.Capability is graph_models.Capability
    assert graph_models.Capability.__module__ == "zeroth.contracts.graph.models"


def test_policy_package_stays_off_the_run_domain_import_path() -> None:
    probe = (
        "import sys\n"
        "import zeroth.governance.policy\n"
        "assert 'zeroth.core.runs' not in sys.modules, 'policy pulled the run domain'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.governance.policy", "zeroth.core.policy"),
        ("zeroth.core.policy", "zeroth.governance.policy"),
        ("zeroth.governance.policy", "zeroth.contracts.graph"),
        ("zeroth.contracts.graph", "zeroth.governance.policy"),
    ],
)
def test_policy_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
