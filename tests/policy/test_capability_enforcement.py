"""WS-C: capability-enforcement helpers, memory guard, and tool-bridge gate."""

from __future__ import annotations

import pytest
from zeroth.core.governed.memory.models import MemoryScope

from zeroth.core.agent_runtime.tools import ToolAttachmentBinding, ToolAttachmentBridge
from zeroth.core.memory.capability_guard import CapabilityEnforcingMemoryConnector
from zeroth.core.memory.connectors import KeyValueMemoryConnector
from zeroth.core.memory.models import ConnectorManifest
from zeroth.core.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.core.policy import (
    Capability,
    CapabilityDeniedError,
    default_capability_registry,
    parse_effective_capabilities,
    require_capabilities,
)


# --- parse_effective_capabilities ------------------------------------------


def test_parse_absent_context_is_empty_set() -> None:
    assert parse_effective_capabilities(None) == set()
    assert parse_effective_capabilities({}) == set()
    assert parse_effective_capabilities({"tenant_id": "t"}) == set()


def test_parse_reads_and_maps_values() -> None:
    ctx = {"effective_capabilities": ["memory_read", "network_write"]}
    assert parse_effective_capabilities(ctx) == {
        Capability.MEMORY_READ,
        Capability.NETWORK_WRITE,
    }


def test_parse_drops_unknown_values_fail_closed() -> None:
    # An unknown grant cannot satisfy any known required capability.
    ctx = {"effective_capabilities": ["memory_read", "not_a_capability"]}
    assert parse_effective_capabilities(ctx) == {Capability.MEMORY_READ}


# --- require_capabilities ---------------------------------------------------


def test_require_noop_when_nothing_required() -> None:
    require_capabilities(set(), None, node_id="n")  # no raise


def test_require_denies_when_missing() -> None:
    with pytest.raises(CapabilityDeniedError) as exc:
        require_capabilities(
            {Capability.SECRET_ACCESS},
            {Capability.MEMORY_READ},
            node_id="n",
        )
    assert Capability.SECRET_ACCESS in exc.value.missing


def test_require_is_fail_closed_on_none_effective() -> None:
    # Absent granted set + a required capability => deny (no None-skip).
    with pytest.raises(CapabilityDeniedError):
        require_capabilities({Capability.NETWORK_WRITE}, None, node_id="n")
    with pytest.raises(CapabilityDeniedError):
        require_capabilities({Capability.NETWORK_WRITE}, set(), node_id="n")


def test_require_allows_when_granted() -> None:
    require_capabilities(
        {Capability.MEMORY_READ},
        {Capability.MEMORY_READ, Capability.MEMORY_WRITE},
        node_id="n",
    )


# --- default capability registry -------------------------------------------


def test_default_registry_resolves_every_capability_by_value() -> None:
    registry = default_capability_registry()
    for capability in Capability:
        assert registry.resolve(capability.value) is capability


# --- CapabilityEnforcingMemoryConnector ------------------------------------


@pytest.mark.asyncio
async def test_memory_guard_read_requires_memory_read() -> None:
    raw = KeyValueMemoryConnector()
    await raw.write("k", {"v": 1}, MemoryScope.SHARED)

    granted = CapabilityEnforcingMemoryConnector(
        raw, effective_capabilities={Capability.MEMORY_READ}, node_id="n"
    )
    entry = await granted.read("k", MemoryScope.SHARED)
    assert entry is not None and entry.value == {"v": 1}

    denied = CapabilityEnforcingMemoryConnector(raw, effective_capabilities=set(), node_id="n")
    with pytest.raises(CapabilityDeniedError):
        await denied.read("k", MemoryScope.SHARED)


@pytest.mark.asyncio
async def test_memory_guard_write_requires_memory_write() -> None:
    raw = KeyValueMemoryConnector()
    read_only = CapabilityEnforcingMemoryConnector(
        raw, effective_capabilities={Capability.MEMORY_READ}, node_id="n"
    )
    with pytest.raises(CapabilityDeniedError):
        await read_only.write("k", {"v": 1}, MemoryScope.SHARED)

    writer = CapabilityEnforcingMemoryConnector(
        raw,
        effective_capabilities={Capability.MEMORY_READ, Capability.MEMORY_WRITE},
        node_id="n",
    )
    await writer.write("k", {"v": 2}, MemoryScope.SHARED)
    assert (await raw.read("k", MemoryScope.SHARED)).value == {"v": 2}


# --- resolver wrapping (WS-C outermost layer) ------------------------------


def _registry() -> InMemoryConnectorRegistry:
    reg = InMemoryConnectorRegistry()
    reg.register(
        "memory://kv",
        ConnectorManifest(connector_type="key_value", scope=MemoryScope.SHARED, instance_id="i"),
        KeyValueMemoryConnector(),
    )
    return reg


@pytest.mark.asyncio
async def test_resolver_none_effective_leaves_connector_ungated() -> None:
    resolver = MemoryConnectorResolver(registry=_registry())
    (binding,) = await resolver.resolve(
        ["memory://kv"],
        runtime_context={"run_id": "r", "tenant_id": "default"},
        effective_capabilities=None,
    )
    # No capability wrapper -> read/write work without any grant.
    await binding.connector.write("k", {"v": 1}, MemoryScope.SHARED)
    assert (await binding.connector.read("k", MemoryScope.SHARED)).value == {"v": 1}


@pytest.mark.asyncio
async def test_resolver_empty_effective_denies_fail_closed() -> None:
    resolver = MemoryConnectorResolver(registry=_registry())
    (binding,) = await resolver.resolve(
        ["memory://kv"],
        runtime_context={"run_id": "r", "tenant_id": "default"},
        node_id="n",
        effective_capabilities=set(),
    )
    with pytest.raises(CapabilityDeniedError):
        await binding.connector.read("k", MemoryScope.SHARED)


@pytest.mark.asyncio
async def test_resolver_granted_effective_allows() -> None:
    resolver = MemoryConnectorResolver(registry=_registry())
    (binding,) = await resolver.resolve(
        ["memory://kv"],
        runtime_context={"run_id": "r", "tenant_id": "default"},
        node_id="n",
        effective_capabilities={Capability.MEMORY_READ, Capability.MEMORY_WRITE},
    )
    await binding.connector.write("k", {"v": 9}, MemoryScope.SHARED)
    assert (await binding.connector.read("k", MemoryScope.SHARED)).value == {"v": 9}


# --- ToolAttachmentBridge.check_capabilities -------------------------------


def _binding(caps: tuple[Capability, ...]) -> ToolAttachmentBinding:
    return ToolAttachmentBinding(
        alias="t", executable_unit_ref="node://u", required_capabilities=caps
    )


def test_tool_check_allows_when_granted() -> None:
    bridge = ToolAttachmentBridge()
    bridge.check_capabilities(
        _binding((Capability.NETWORK_WRITE,)),
        {Capability.NETWORK_WRITE},
        node_id="agent",
    )


def test_tool_check_denies_when_missing() -> None:
    bridge = ToolAttachmentBridge()
    with pytest.raises(CapabilityDeniedError):
        bridge.check_capabilities(_binding((Capability.SECRET_ACCESS,)), set(), node_id="agent")


def test_tool_check_noop_when_tool_requires_nothing() -> None:
    bridge = ToolAttachmentBridge()
    bridge.check_capabilities(_binding(()), set(), node_id="agent")  # no raise
