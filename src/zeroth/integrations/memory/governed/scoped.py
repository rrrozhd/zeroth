"""ScopedMemoryConnector: pre-fills scope targets from execution context."""

from __future__ import annotations

from zeroth.integrations.memory.governed.connector import MemoryConnector
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope
from zeroth.contracts.governed.models.common import JSONValue


class ScopedMemoryConnector:
    """Thin wrapper that pre-fills scope targets from execution context.

    Per D-03: thread scope defaults to current thread_id,
    run scope to current run_id. Explicit target override available.
    Per D-20: Tools access this via ctx.memory.
    """

    def __init__(
        self,
        connector: MemoryConnector,
        *,
        run_id: str,
        thread_id: str | None = None,
        workflow_name: str,
    ) -> None:
        self._connector = connector
        self._run_id = run_id
        self._thread_id = thread_id
        self._workflow_name = workflow_name

    def _resolve_target(self, scope: MemoryScope, target: str | None) -> str:
        if target is not None:
            return target
        if scope == MemoryScope.RUN:
            return self._run_id
        if scope == MemoryScope.THREAD:
            return self._thread_id or self._run_id
        return "__shared__"

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        return await self._connector.read(
            key, scope, target=self._resolve_target(scope, target)
        )

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        await self._connector.write(
            key, value, scope, target=self._resolve_target(scope, target)
        )

    async def delete(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        await self._connector.delete(
            key, scope, target=self._resolve_target(scope, target)
        )

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        return await self._connector.search(
            query, scope, target=self._resolve_target(scope, target)
        )
