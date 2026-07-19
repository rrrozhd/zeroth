"""AuditingMemoryConnector: audit-emitting decorator for MemoryConnector."""

from __future__ import annotations

from typing import Any

from zeroth.core.governed.audit.emitter import AuditEmitter, emit_event
from zeroth.core.governed.memory.connector import MemoryConnector
from zeroth.core.governed.memory.models import MemoryEntry, MemoryScope
from zeroth.contracts.governed.models.common import EventType, JSONValue


class AuditingMemoryConnector:
    """Decorates a MemoryConnector to emit audit events for all operations.

    Per D-13: Wrapping decorator pattern (mirrors RedactingAuditEmitter).
    Per D-18: Uses emit_event() helper for consistent audit emission.
    Per D-19: Does NOT register memory values with SecretRegistry.
    """

    def __init__(
        self,
        inner: MemoryConnector,
        emitter: AuditEmitter,
        *,
        run_id: str,
        thread_id: str | None = None,
        workflow_name: str,
    ) -> None:
        self._inner = inner
        self._emitter = emitter
        self._run_id = run_id
        self._thread_id = thread_id
        self._workflow_name = workflow_name

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        result = await self._inner.read(key, scope, target=target)
        await emit_event(
            self._emitter,
            run_id=self._run_id,
            thread_id=self._thread_id,
            workflow_name=self._workflow_name,
            event_type=EventType.MEMORY_READ,
            payload={"key": key, "scope": scope.value, "found": result is not None},
        )
        return result

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        # Check existence first to determine created flag (D-16)
        existing = await self._inner.read(key, scope, target=target)
        await self._inner.write(key, value, scope, target=target)
        # CRITICAL: payload must NOT contain "value" (D-15)
        await emit_event(
            self._emitter,
            run_id=self._run_id,
            thread_id=self._thread_id,
            workflow_name=self._workflow_name,
            event_type=EventType.MEMORY_WRITE,
            payload={"key": key, "scope": scope.value, "created": existing is None},
        )

    async def delete(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        found = True
        try:
            await self._inner.delete(key, scope, target=target)
        except KeyError:
            found = False
            raise
        finally:
            await emit_event(
                self._emitter,
                run_id=self._run_id,
                thread_id=self._thread_id,
                workflow_name=self._workflow_name,
                event_type=EventType.MEMORY_DELETE,
                payload={"key": key, "scope": scope.value, "found": found},
            )

    async def search(
        self, query: dict[str, Any], scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        results = await self._inner.search(query, scope, target=target)
        await emit_event(
            self._emitter,
            run_id=self._run_id,
            thread_id=self._thread_id,
            workflow_name=self._workflow_name,
            event_type=EventType.MEMORY_SEARCH,
            payload={"query": query, "scope": scope.value, "result_count": len(results)},
        )
        return results
