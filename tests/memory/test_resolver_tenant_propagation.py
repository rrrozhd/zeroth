"""WS-B: the resolver reads tenant per-call and isolates across tenants.

The resolver is a bootstrap SINGLETON shared by every tenant, so tenant must be
read from ``runtime_context`` on each ``resolve`` call — never stored on
``__init__``. Two resolves with different tenants on the SAME resolver instance
and the SAME registered raw connector must not see each other's memory.
"""

from __future__ import annotations

import pytest
from zeroth.integrations.memory.governed.models import MemoryScope

from zeroth.integrations.memory.connectors import KeyValueMemoryConnector
from zeroth.integrations.memory.models import ConnectorManifest
from zeroth.integrations.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.integrations.memory.tenant_scoped import TenantScopeError


def _resolver() -> tuple[MemoryConnectorResolver, KeyValueMemoryConnector]:
    raw = KeyValueMemoryConnector()
    registry = InMemoryConnectorRegistry()
    registry.register(
        "memory://kv",
        ConnectorManifest(connector_type="key_value", scope=MemoryScope.SHARED),
        raw,
    )
    return MemoryConnectorResolver(registry=registry, workflow_name="wf"), raw


@pytest.mark.asyncio
async def test_two_tenants_same_singleton_are_isolated():
    resolver, _raw = _resolver()

    a = (
        await resolver.resolve(
            ["memory://kv"], runtime_context={"run_id": "r", "tenant_id": "tenant-a"}
        )
    )[0].connector
    b = (
        await resolver.resolve(
            ["memory://kv"], runtime_context={"run_id": "r", "tenant_id": "tenant-b"}
        )
    )[0].connector

    await a.write("k", {"v": "a"}, MemoryScope.SHARED)
    assert (await a.read("k", MemoryScope.SHARED)).value == {"v": "a"}
    # Same resolver, same raw connector, same run_id — only tenant differs.
    assert await b.read("k", MemoryScope.SHARED) is None


@pytest.mark.asyncio
async def test_resolver_fails_closed_without_tenant():
    resolver, _raw = _resolver()
    with pytest.raises(TenantScopeError):
        await resolver.resolve(["memory://kv"], runtime_context={"run_id": "r"})


@pytest.mark.asyncio
async def test_resolve_accepts_effective_capabilities_kwarg():
    # WS-C will pass effective_capabilities to the (future) outermost
    # CapabilityEnforcing wrapper; the additive keyword-only param must already
    # be accepted so WS-C's wiring is a one-line edit.
    resolver, _raw = _resolver()
    bindings = await resolver.resolve(
        ["memory://kv"],
        runtime_context={"run_id": "r", "tenant_id": "default"},
        effective_capabilities=None,
    )
    assert len(bindings) == 1
