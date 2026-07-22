"""Capability-enforcing wrapper for memory connectors (WS-C).

Memory access is one of the capabilities the platform governs
(``MEMORY_READ`` / ``MEMORY_WRITE``). Until WS-C those capabilities were only
*audited*; this connector makes them a behavioral gate.

``CapabilityEnforcingMemoryConnector`` is the **outermost** layer of the
resolver's wrapper stack::

    Capability(Scoped(TenantScoped(Auditing(raw))))

so the capability check runs BEFORE scope/tenant resolution, auditing, or the
raw backend ever see the call. Reads (``read`` / ``search``) require
``MEMORY_READ``; mutations (``write`` / ``delete``) require ``MEMORY_WRITE``.
A missing capability raises :class:`CapabilityDeniedError` and the delegate is
never touched. It is constructed only when enforcement is active (the resolver
passes a non-None ``effective_capabilities`` set), so an empty granted set here
means "declared nothing" and therefore denies — fail-closed.
"""

from __future__ import annotations

from zeroth.contracts.governed.models.common import JSONValue
from zeroth.governance.policy.errors import require_capabilities
from zeroth.governance.policy.models import Capability
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope


class CapabilityEnforcingMemoryConnector:
    """Gate a memory connector on the node's granted MEMORY_READ/WRITE capabilities.

    Implements the governed ``MemoryConnector`` protocol
    (``read`` / ``write`` / ``delete`` / ``search`` with a keyword-only
    ``target``). Instances are created per-resolve, binding the granted set and
    node id at construction — never on shared singletons.
    """

    def __init__(
        self,
        inner: object,
        *,
        effective_capabilities: set[Capability],
        node_id: str,
    ) -> None:
        self._inner = inner
        self._granted = set(effective_capabilities)
        self._node_id = node_id

    def _require(self, capability: Capability) -> None:
        require_capabilities({capability}, self._granted, node_id=self._node_id)

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        self._require(Capability.MEMORY_READ)
        return await self._inner.read(key, scope, target=target)

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        self._require(Capability.MEMORY_READ)
        return await self._inner.search(query, scope, target=target)

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        self._require(Capability.MEMORY_WRITE)
        await self._inner.write(key, value, scope, target=target)

    async def delete(self, key: str, scope: MemoryScope, *, target: str | None = None) -> None:
        self._require(Capability.MEMORY_WRITE)
        await self._inner.delete(key, scope, target=target)


__all__ = ["CapabilityEnforcingMemoryConnector"]
