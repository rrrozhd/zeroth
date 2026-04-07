"""Tests for MemoryConnectorResolver with GovernAI wrapping.

Verifies that the resolver wraps connectors with AuditingMemoryConnector
then ScopedMemoryConnector, and returns correct ResolvedMemoryBinding shape.
"""

from __future__ import annotations

import pytest
from governai.audit.emitter import AuditEmitter
from governai.memory.models import MemoryScope

from zeroth.memory.connectors import KeyValueMemoryConnector, RunEphemeralMemoryConnector
from zeroth.memory.models import ConnectorManifest, ResolvedMemoryBinding
from zeroth.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver


class FakeAuditEmitter(AuditEmitter):
    """Test double that records emitted events."""

    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:  # type: ignore[override]
        self.events.append(event)


@pytest.fixture
def registry() -> InMemoryConnectorRegistry:
    reg = InMemoryConnectorRegistry()
    reg.register(
        "memory://kv",
        ConnectorManifest(
            connector_type="key_value",
            scope=MemoryScope.SHARED,
            instance_id="shared-inst",
        ),
        KeyValueMemoryConnector(),
    )
    reg.register(
        "memory://ephemeral",
        ConnectorManifest(
            connector_type="ephemeral",
            scope=MemoryScope.RUN,
        ),
        RunEphemeralMemoryConnector(),
    )
    return reg


@pytest.mark.asyncio
async def test_resolver_wraps_with_scoped_and_auditing(registry: InMemoryConnectorRegistry) -> None:
    emitter = FakeAuditEmitter()
    resolver = MemoryConnectorResolver(
        registry=registry,
        audit_emitter=emitter,
        workflow_name="test-wf",
    )
    bindings = await resolver.resolve(
        ["memory://kv"],
        runtime_context={"run_id": "run-1"},
        thread_id="t-1",
    )
    assert len(bindings) == 1
    binding = bindings[0]
    assert isinstance(binding, ResolvedMemoryBinding)
    assert binding.memory_ref == "memory://kv"
    # Connector should be wrapped -- test by using it
    await binding.connector.write("test-key", {"data": 1}, MemoryScope.SHARED)
    entry = await binding.connector.read("test-key", MemoryScope.SHARED)
    assert entry is not None
    assert entry.value == {"data": 1}
    # Audit events should have been emitted (write checks existence first = read + write + read)
    assert len(emitter.events) >= 2


@pytest.mark.asyncio
async def test_resolver_without_emitter_still_wraps_scoped(registry: InMemoryConnectorRegistry) -> None:
    resolver = MemoryConnectorResolver(
        registry=registry,
        workflow_name="test-wf",
    )
    bindings = await resolver.resolve(
        ["memory://ephemeral"],
        runtime_context={"run_id": "run-1"},
    )
    assert len(bindings) == 1
    binding = bindings[0]
    # Should work without auditing
    await binding.connector.write("k", "v", MemoryScope.RUN)
    entry = await binding.connector.read("k", MemoryScope.RUN)
    assert entry is not None
    assert entry.value == "v"


@pytest.mark.asyncio
async def test_resolved_binding_has_no_context_field(registry: InMemoryConnectorRegistry) -> None:
    resolver = MemoryConnectorResolver(registry=registry, workflow_name="test-wf")
    bindings = await resolver.resolve(
        ["memory://kv"],
        runtime_context={"run_id": "run-1"},
    )
    binding = bindings[0]
    assert not hasattr(binding, "context") or "context" not in binding.model_fields


@pytest.mark.asyncio
async def test_connector_manifest_uses_memory_scope(registry: InMemoryConnectorRegistry) -> None:
    resolver = MemoryConnectorResolver(registry=registry, workflow_name="test-wf")
    bindings = await resolver.resolve(
        ["memory://kv"],
        runtime_context={"run_id": "run-1"},
    )
    assert bindings[0].manifest.scope == MemoryScope.SHARED
