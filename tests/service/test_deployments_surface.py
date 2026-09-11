"""Canonical import surface for the service deployments package.

Non-golden boundary tests for the Task 16 deployments move: the canonical
``zeroth.service.deployments`` package must publish the same objects the
legacy ``zeroth.core.deployments`` path keeps republishing, and both
packages must stay cold-importable from a fresh interpreter in either
order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

EXPORTS = (
    "Deployment",
    "DeploymentError",
    "DeploymentService",
    "DeploymentStatus",
    "SQLiteDeploymentRepository",
)


def test_deployments_publishes_its_whole_surface() -> None:
    from zeroth.service import deployments as canonical

    for name in EXPORTS:
        assert hasattr(canonical, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("models", ("Deployment", "DeploymentStatus")),
        (
            "provenance",
            (
                "build_attestation_payload",
                "compute_attestation_digest",
                "compute_contract_snapshot_digest",
                "compute_graph_snapshot_digest",
                "compute_settings_snapshot_digest",
                "sign_attestation",
                "verify_attestation",
                "verify_attestation_full",
                "verify_attestation_signature",
            ),
        ),
        (
            "repository",
            ("DeploymentRefLineageConflictError", "SQLiteDeploymentRepository"),
        ),
        ("service", ("DeploymentError", "DeploymentService")),
    ],
)
def test_deployments_modules_publish_their_names(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    canonical_module = importlib.import_module(f"zeroth.service.deployments.{module_name}")

    for name in names:
        assert hasattr(canonical_module, name), name


def test_deployments_imports_in_a_cold_interpreter() -> None:
    """The canonical package imports with nothing else pre-warmed.

    This kept the canonical half of a test that used to import the legacy
    and canonical packages in both orders, guarding a cycle between them.
    With the legacy package gone there is one direction left to guard.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.service.deployments"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
