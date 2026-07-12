"""WS-C: bootstrap wires a default PolicyGuard behind settings.policy.enforce_capabilities."""

from __future__ import annotations

import pytest

from tests.service.helpers import agent_graph, deploy_service
from zeroth.core.config.settings import get_settings


@pytest.mark.asyncio
async def test_default_bootstrap_wires_policy_guard(sqlite_db) -> None:
    # Default settings enforce capabilities -> a non-None guard is wired.
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="ws-c-guard-on"))
    assert service.orchestrator.policy_guard is not None
    # The default guard resolves the served ref scheme (capability values).
    from zeroth.core.policy import Capability

    registry = service.orchestrator.policy_guard.capability_registry
    assert registry.resolve("memory_read") is Capability.MEMORY_READ


@pytest.mark.asyncio
async def test_bootstrap_leaves_guard_unset_when_enforcement_disabled(
    sqlite_db, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings.policy, "enforce_capabilities", False)
    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="ws-c-guard-off"))
    assert service.orchestrator.policy_guard is None
