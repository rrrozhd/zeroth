"""WS-C: the LOCAL sandbox refuses network-bearing nodes under strict posture.

Behavioral network isolation needs Docker/sidecar. Under STRICT (and STANDARD),
a local subprocess cannot honor a network constraint, so the backend resolver
refuses rather than running unconstrained — the README's LOCAL caveat made real.
"""

from __future__ import annotations

import pytest

from zeroth.integrations.execution.constraints import ResourceConstraints
from zeroth.integrations.execution.sandbox import (
    SandboxBackendMode,
    SandboxConfig,
    SandboxManager,
    SandboxPolicyViolationError,
    SandboxStrictnessMode,
)


def _local_manager(strictness: SandboxStrictnessMode) -> SandboxManager:
    return SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.LOCAL, strictness_mode=strictness)
    )


def test_strict_local_refuses_network_bearing_node() -> None:
    with pytest.raises(SandboxPolicyViolationError):
        _local_manager(SandboxStrictnessMode.STRICT).run(
            ["echo", "hi"],
            resource_constraints=ResourceConstraints(network_access=True),
        )


def test_standard_local_also_refuses_network_bearing_node() -> None:
    # STANDARD is at least as strict as asked: it, too, refuses local network nodes.
    with pytest.raises(SandboxPolicyViolationError):
        _local_manager(SandboxStrictnessMode.STANDARD).run(
            ["echo", "hi"],
            resource_constraints=ResourceConstraints(network_access=False),
        )


def test_permissive_local_allows_network_bearing_node(tmp_path) -> None:
    # The escape hatch: PERMISSIVE runs locally (a warning is emitted, not a raise).
    result = _local_manager(SandboxStrictnessMode.PERMISSIVE).run(
        ["echo", "hi"],
        resource_constraints=ResourceConstraints(network_access=True),
        working_directory=None,
    )
    assert result.returncode == 0
