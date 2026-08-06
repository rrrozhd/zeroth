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


def test_policy_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.governance.policy as canonical

    expected = {
        "Capability",
        "CapabilityDeniedError",
        "CapabilityRegistry",
        "EnforcementResult",
        "PolicyDecision",
        "PolicyDefinition",
        "PolicyGuard",
        "PolicyRegistry",
        "apply_secret_policy",
        "default_capability_registry",
        "parse_effective_capabilities",
        "require_capabilities",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.governance.policy no longer publishes: {missing}"


def test_policy_submodules_publish_their_names() -> None:
    from zeroth.governance.policy import errors as canonical_errors
    from zeroth.governance.policy import guard as canonical_guard
    from zeroth.governance.policy import models as canonical_models
    from zeroth.governance.policy import registry as canonical_registry

    assert hasattr(canonical_models, "Capability")
    assert hasattr(canonical_models, "EnforcementResult")
    assert hasattr(canonical_models, "PolicyDecision")
    assert hasattr(canonical_models, "PolicyDefinition")
    assert hasattr(canonical_errors, "CapabilityDeniedError")
    assert hasattr(canonical_guard, "PolicyGuard")
    assert hasattr(canonical_guard, "apply_secret_policy")
    assert hasattr(canonical_registry, "CapabilityRegistry")
    assert hasattr(canonical_registry, "PolicyRegistry")


def test_capability_is_defined_in_the_graph_contracts() -> None:
    from zeroth.contracts import graph as graph_package
    from zeroth.contracts.graph import models as graph_models
    from zeroth.governance.policy import models as canonical_models

    assert canonical_models.Capability is graph_models.Capability
    assert hasattr(graph_models, "Capability")
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


def test_policy_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.governance.policy"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
