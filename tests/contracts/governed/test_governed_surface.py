"""Canonical import surface for the governed contracts package.

Boundary tests for the contract slice of the vendored governai bundle: the
canonical ``zeroth.contracts.governed`` package publishes the app spec and the
contract models, and it stays cold-importable together with the run surfaces
that share its vocabulary.

These began as parity tests against the legacy ``zeroth.core.governed`` paths.
ZER-25 removed those paths, so the comparisons would compare each module with
itself. What they were pinning -- the exported surface, and the fact that the
governed models are the *same objects* the run surface publishes -- is asserted
directly instead. Only the contract slice moved; the audit, integrations,
memory, runtime and tools implementations stay put
(see docs/backend-import-migration.md).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

CANONICAL_MODULES = (
    "zeroth.contracts.governed",
    "zeroth.contracts.governed.app.spec",
    "zeroth.contracts.governed.models.approval",
    "zeroth.contracts.governed.models.audit",
    "zeroth.contracts.governed.models.common",
    "zeroth.contracts.governed.models.run_state",
    "zeroth.runtime.runs",
)


def test_governed_aggregator_publishes_the_contract_models() -> None:
    from zeroth.contracts import governed as canonical

    assert canonical.RunState.__name__ == "RunState"
    assert canonical.RunStatus.__name__ == "RunStatus"


def test_governed_spec_publishes_its_whole_surface() -> None:
    from zeroth.contracts.governed.app import spec

    expected = {
        "ChannelSpec",
        "GovernedFlowSpec",
        "GovernedStepSpec",
        "InterruptContract",
        "TransitionSpec",
        "branch",
        "end",
        "route_to",
        "then",
    }

    missing = sorted(name for name in expected if not hasattr(spec, name))
    assert not missing, f"governed app spec no longer publishes: {missing}"


def test_governed_models_publish_their_whole_surface() -> None:
    from zeroth.contracts.governed.models import approval, audit, common, run_state

    expected = {
        approval: {"ApprovalDecision", "ApprovalDecisionType", "ApprovalRequest"},
        audit: {"AuditEvent", "AuditExtension"},
        common: {
            "DeterminismMode",
            "END_STEP",
            "EventType",
            "JSONValue",
            "RunStatus",
            "normalize_step_ref",
        },
        run_state: {"RunState"},
    }

    missing = sorted(
        f"{module.__name__}:{name}"
        for module, names in expected.items()
        for name in names
        if not hasattr(module, name)
    )
    assert not missing, f"governed models no longer publish: {missing}"


def test_the_run_surface_publishes_the_governed_contract_models_themselves() -> None:
    """The run domain must not fork the governed vocabulary.

    This is the assertion the parity test actually existed for, and it survives
    the removal unchanged: ``zeroth.runtime.runs`` republishes the *same class
    objects* the governed contracts define, rather than declaring its own.
    """
    from zeroth.contracts import governed as canonical
    from zeroth.runtime import runs as runtime_runs

    assert runtime_runs.RunState is canonical.RunState
    assert runtime_runs.RunStatus is canonical.RunStatus


@pytest.mark.parametrize("module", CANONICAL_MODULES)
def test_governed_modules_import_in_a_cold_interpreter(module: str) -> None:
    """Each module imports with nothing else pre-warmed.

    The original ran every ordered pair of canonical and legacy packages to
    catch a cycle between them. With the legacy packages gone, what remains
    worth guarding is that each canonical module stands up on its own.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"cold import of {module} failed:\n{result.stderr}"
