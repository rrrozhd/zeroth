"""Canonical import surface for the governed contracts package.

Non-golden boundary tests for the Task 12 governed move: the canonical
``zeroth.contracts.governed`` package must publish the same objects the
legacy ``zeroth.core.governed`` app and models paths keep republishing, and
both packages must stay cold-importable from a fresh interpreter in either
order. Only the contract slice of the vendored governai bundle moves — the
audit, integrations, memory, runtime, and tools implementations stay put
(see docs/governed-capability-disposition.md).
"""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_governed_aggregator_republishes_the_contract_models() -> None:
    from zeroth.contracts import governed as canonical
    from zeroth.core import governed as legacy

    assert canonical.RunState is legacy.RunState
    assert canonical.RunStatus is legacy.RunStatus


def test_governed_spec_is_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts.governed.app import spec as canonical
    from zeroth.core.governed.app import spec as legacy

    assert canonical.ChannelSpec is legacy.ChannelSpec
    assert canonical.GovernedFlowSpec is legacy.GovernedFlowSpec
    assert canonical.GovernedStepSpec is legacy.GovernedStepSpec
    assert canonical.InterruptContract is legacy.InterruptContract
    assert canonical.TransitionSpec is legacy.TransitionSpec
    assert canonical.branch is legacy.branch
    assert canonical.end is legacy.end
    assert canonical.route_to is legacy.route_to
    assert canonical.then is legacy.then


def test_governed_models_are_the_same_surface_through_both_paths() -> None:
    from zeroth.contracts.governed.models import approval as canonical_approval
    from zeroth.contracts.governed.models import audit as canonical_audit
    from zeroth.contracts.governed.models import common as canonical_common
    from zeroth.contracts.governed.models import run_state as canonical_run_state
    from zeroth.core.governed.models import approval as legacy_approval
    from zeroth.core.governed.models import audit as legacy_audit
    from zeroth.core.governed.models import common as legacy_common
    from zeroth.core.governed.models import run_state as legacy_run_state

    assert canonical_approval.ApprovalDecision is legacy_approval.ApprovalDecision
    assert canonical_approval.ApprovalDecisionType is legacy_approval.ApprovalDecisionType
    assert canonical_approval.ApprovalRequest is legacy_approval.ApprovalRequest
    assert canonical_audit.AuditEvent is legacy_audit.AuditEvent
    assert canonical_audit.AuditExtension is legacy_audit.AuditExtension
    assert canonical_common.DeterminismMode is legacy_common.DeterminismMode
    assert canonical_common.END_STEP is legacy_common.END_STEP
    assert canonical_common.EventType is legacy_common.EventType
    assert canonical_common.JSONValue == legacy_common.JSONValue
    assert canonical_common.RunStatus is legacy_common.RunStatus
    assert canonical_common.normalize_step_ref is legacy_common.normalize_step_ref
    assert canonical_run_state.RunState is legacy_run_state.RunState


def test_run_surface_keeps_republishing_the_governed_contract_models() -> None:
    from zeroth.contracts import governed as canonical
    from zeroth.core.runs import models as legacy_run_models
    from zeroth.runtime import runs as runtime_runs

    assert legacy_run_models.RunState is canonical.RunState
    assert legacy_run_models.RunStatus is canonical.RunStatus
    assert runtime_runs.RunState is canonical.RunState
    assert runtime_runs.RunStatus is canonical.RunStatus


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.contracts.governed", "zeroth.core.governed"),
        ("zeroth.core.governed", "zeroth.contracts.governed"),
        ("zeroth.contracts.governed", "zeroth.core.runs"),
        ("zeroth.core.runs", "zeroth.contracts.governed"),
        ("zeroth.contracts.governed", "zeroth.runtime.runs"),
        ("zeroth.runtime.runs", "zeroth.contracts.governed"),
    ],
)
def test_governed_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
