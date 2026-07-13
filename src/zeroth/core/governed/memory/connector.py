"""MemoryConnector Protocol: async interface for memory backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from zeroth.core.governed.memory.models import MemoryEntry, MemoryScope
from zeroth.core.governed.models.common import JSONValue


@runtime_checkable
class MemoryConnector(Protocol):
    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None: ...

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None: ...

    async def delete(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> None: ...

    async def search(
        self, query: dict, scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]: ...
