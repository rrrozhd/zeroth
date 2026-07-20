"""Runtime-owned protocols for the memory seams agent execution drives.

Runtime code must not import concrete integration modules; it names the
integration objects it drives through these structural contracts instead.
The concrete implementations live in the memory integrations package and
are constructed and injected by service bootstrap.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from zeroth.governance.policy.models import Capability


class MemoryConnectorResolver(Protocol):
    """Structural contract for the memory resolver the agent runner drives.

    The concrete resolver (``zeroth.integrations.memory.registry``) satisfies
    this protocol. The name deliberately matches the concrete class: the
    ``AgentRunner`` ``__init__`` annotation that mentions it is pinned by the
    immutable legacy surface, and signature comparison keeps the bare type
    name while dropping Zeroth module qualifiers.
    """

    async def resolve(
        self,
        memory_refs: list[str],
        *,
        thread_id: str | None = None,
        runtime_context: Mapping[str, Any] | None = None,
        node_id: str | None = None,
        effective_capabilities: set[Capability] | None = None,
    ) -> Sequence[Any]:
        """Resolve memory ref names into ready-to-use bindings."""
        ...
