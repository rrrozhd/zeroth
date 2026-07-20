"""Runtime-owned protocols for the execution seams orchestration drives.

Runtime code must not import concrete integration modules; it names the
integration objects it drives through these structural contracts instead.
The concrete implementations live in the execution integrations package and
are constructed and injected by service bootstrap.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class ExecutableUnitRunner(Protocol):
    """Structural contract for the executable-unit runner the runtime drives.

    The concrete runner (``zeroth.core.execution_units.runner``) satisfies
    this protocol. The name deliberately matches the concrete class: the
    ``RuntimeOrchestrator`` ``__init__`` annotation that mentions it is pinned
    by the immutable legacy surface, and signature comparison keeps the bare
    type name while dropping Zeroth module qualifiers.
    """

    secret_resolver: Any

    async def run(
        self,
        manifest_ref: str,
        payload: Any,
        *,
        enforcement_context: Mapping[str, Any] | None = None,
    ) -> Any:
        """Run a unit by its manifest ref string."""
        ...

    async def run_binding(
        self,
        binding: Any,
        payload: Any,
        *,
        enforcement_context: Mapping[str, Any] | None = None,
    ) -> Any:
        """Run an executable unit from a binding directly."""
        ...
