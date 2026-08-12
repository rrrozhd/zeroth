from __future__ import annotations

import inspect

from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.service.langgraph_gateway import enforcement as _enforcement  # noqa: F401
from zeroth.service.langgraph_gateway.enforcement_store import LangGraphEnforcementRepository


def test_langgraph_repository_constructor_requires_scope_context() -> None:
    parameters = inspect.signature(LangGraphEnforcementRepository).parameters

    assert "scope_context" in parameters
    assert parameters["scope_context"].default is inspect.Parameter.empty


async def test_foreign_langgraph_attestation_matches_unknown_scope(async_database) -> None:
    owner = LangGraphEnforcementRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    foreign = LangGraphEnforcementRepository(
        async_database, NullWorkspaceScopeContext(tenant_id="tenant-b")
    )
    payload = {
        "tenant_id": "tenant-a",
        "deployment_ref": "deployment-a",
        "run_id": "shared-run",
        "correlation_id": "shared-correlation",
        "governance_level": "enforced",
        "observed_at": "2026-08-12T00:00:00+00:00",
        "graph_version": "graph:v1",
        "adapter_version": "adapter:v1",
        "inventory_fingerprint": "fingerprint",
        "tool_manifest_complete": True,
    }
    await owner.save_attestation(payload, b"signature", "key-1", "hmac-sha256")

    assert await foreign.get_attestation_by_run_id("deployment-a", "shared-run") is None
    assert await foreign.get_attestation_by_run_id("deployment-a", "unknown-run") is None
