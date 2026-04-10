"""Registry and resolver for memory connectors.

The registry stores known connectors by name. The resolver takes a list of
memory reference names and turns them into ready-to-use bindings (connector
wrapped with ScopedMemoryConnector and optionally AuditingMemoryConnector).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from governai.audit.emitter import AuditEmitter
from governai.memory.auditing import AuditingMemoryConnector
from governai.memory.models import MemoryScope
from governai.memory.scoped import ScopedMemoryConnector

from zeroth.core.memory.models import (
    ConnectorManifest,
    ResolvedMemoryBinding,
)
from zeroth.core.runs import ThreadMemoryBinding, ThreadRepository


class InMemoryConnectorRegistry:
    """A simple lookup table that maps memory ref names to connectors.

    You register connectors by name, then look them up later when you
    need to read or write memory. Raises KeyError if a name isn't found.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[ConnectorManifest, Any]] = {}

    def register(
        self,
        memory_ref: str,
        manifest: ConnectorManifest,
        connector: Any,
    ) -> None:
        """Add a connector to the registry under the given name."""
        self._entries[memory_ref] = (manifest, connector)

    def resolve(self, memory_ref: str) -> tuple[ConnectorManifest, Any]:
        """Look up a connector by name. Raises KeyError if not registered."""
        try:
            return self._entries[memory_ref]
        except KeyError as exc:
            raise KeyError(memory_ref) from exc


class MemoryConnectorResolver:
    """Turns memory ref names into fully resolved, ready-to-use bindings.

    Given a list of memory reference names, this resolver looks each one up
    in the registry, wraps the raw connector with AuditingMemoryConnector
    (if an emitter is provided) and ScopedMemoryConnector, and returns
    ResolvedMemoryBinding instances.
    """

    def __init__(
        self,
        *,
        registry: InMemoryConnectorRegistry | None = None,
        thread_repository: ThreadRepository | None = None,
        audit_emitter: AuditEmitter | None = None,
        workflow_name: str = "",
    ) -> None:
        self.registry = registry or InMemoryConnectorRegistry()
        self.thread_repository = thread_repository
        self._audit_emitter = audit_emitter
        self._workflow_name = workflow_name

    async def resolve(
        self,
        memory_refs: list[str],
        *,
        thread_id: str | None = None,
        runtime_context: Mapping[str, Any] | None = None,
        node_id: str | None = None,
    ) -> list[ResolvedMemoryBinding]:
        """Resolve a list of memory ref names into ready-to-use bindings.

        For each ref, looks up the connector, wraps it with Auditing + Scoped
        wrappers, and returns the complete binding.
        """
        runtime_context = dict(runtime_context or {})
        run_id = runtime_context.get("run_id", "unknown")
        bindings: list[ResolvedMemoryBinding] = []
        for memory_ref in memory_refs:
            manifest, raw_connector = self.registry.resolve(memory_ref)

            # Wrap with AuditingMemoryConnector if emitter is available
            wrapped = raw_connector
            if self._audit_emitter is not None:
                wrapped = AuditingMemoryConnector(
                    wrapped,
                    self._audit_emitter,
                    run_id=run_id,
                    thread_id=thread_id,
                    workflow_name=self._workflow_name,
                )

            # Wrap with ScopedMemoryConnector for automatic target resolution
            wrapped = ScopedMemoryConnector(
                wrapped,
                run_id=run_id,
                thread_id=thread_id,
                workflow_name=self._workflow_name,
            )

            bindings.append(
                ResolvedMemoryBinding(
                    memory_ref=memory_ref,
                    manifest=manifest,
                    connector=wrapped,
                )
            )
            await self._record_thread_binding(
                memory_ref, manifest, run_id=run_id, thread_id=thread_id
            )
        return bindings

    def _instance_id(
        self,
        manifest: ConnectorManifest,
        *,
        memory_ref: str,
        run_id: str | None,
        thread_id: str | None,
    ) -> str:
        """Figure out the right instance ID based on scope and available IDs."""
        if manifest.instance_id is not None:
            return manifest.instance_id
        if manifest.scope is MemoryScope.RUN:
            return run_id or f"{memory_ref}:run"
        if manifest.scope is MemoryScope.THREAD:
            return thread_id or f"{memory_ref}:thread"
        return memory_ref

    async def _record_thread_binding(
        self,
        memory_ref: str,
        manifest: ConnectorManifest,
        *,
        run_id: str,
        thread_id: str | None,
    ) -> None:
        """Save the memory binding to the thread repository for tracking."""
        repository = self.thread_repository
        if repository is None or thread_id is None:
            return
        thread = await repository.get(thread_id)
        if thread is None:
            return
        instance_id = self._instance_id(
            manifest, memory_ref=memory_ref, run_id=run_id, thread_id=thread_id
        )
        binding = ThreadMemoryBinding(
            connector_id=memory_ref,
            instance_id=instance_id,
            scope=manifest.scope.value,
        )
        if binding in thread.memory_bindings:
            return
        thread.memory_bindings.append(binding)
        await repository.update(thread)
