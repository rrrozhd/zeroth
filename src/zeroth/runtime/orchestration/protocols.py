"""Runtime-owned protocols for the execution seams orchestration drives.

Runtime code must not import concrete integration modules; it names the
integration objects it drives through these structural contracts instead.
The concrete implementations live in the execution integrations package and
are constructed and injected by service bootstrap.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from zeroth.contracts.graph import OperationIdentity
from zeroth.runtime.runs.protocols import CheckpointStore, RunReader, RunWriter


class RunRepository(RunReader, RunWriter, CheckpointStore, Protocol):
    """Structural contract for the run store the orchestrator persists through.

    The concrete repository (``zeroth.integrations.persistence.runs``) satisfies
    this protocol. As with :class:`ExecutableUnitRunner`, the name deliberately
    matches the concrete class: the ``RuntimeOrchestrator`` ``__init__``
    annotation that mentions it is pinned by the immutable legacy surface, and
    signature comparison keeps the bare type name while dropping Zeroth module
    qualifiers.

    It is exactly the union of the three run protocols the orchestrator drives
    -- it reads a run, creates and puts it, and writes checkpoints -- so naming
    it here lets the runtime keep its dependency direction instead of importing
    the persistence adapter it is handed.
    """


class ExecutableUnitRunner(Protocol):
    """Structural contract for the executable-unit runner the runtime drives.

    The concrete runner (``zeroth.integrations.execution.runner``) satisfies
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
        operation_identity: OperationIdentity | None = None,
    ) -> Any:
        """Run a unit by its manifest ref string.

        ``operation_identity`` names the logical side-effecting operation. It is
        declared here by exact name because the executor offers optional kwargs
        by signature inspection -- a runner that does not declare it silently
        never receives it, which is precisely how it went missing before.
        """
        ...

    async def run_binding(
        self,
        binding: Any,
        payload: Any,
        *,
        enforcement_context: Mapping[str, Any] | None = None,
        operation_identity: OperationIdentity | None = None,
    ) -> Any:
        """Run an executable unit from a binding directly."""
        ...

    async def run_inline_source(
        self,
        unit_id: str,
        source: str,
        payload: Any,
        *,
        timeout_seconds: int | None = None,
        enforcement_context: Mapping[str, Any] | None = None,
        operation_identity: OperationIdentity | None = None,
    ) -> Any:
        """Run inline source authored in a graph node, binding it on demand."""
        ...
