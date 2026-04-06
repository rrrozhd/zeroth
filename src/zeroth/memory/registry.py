"""Registry and resolver for memory connectors.

The registry stores known connectors by name. The resolver takes a list of
memory reference names and turns them into ready-to-use bindings (connector +
context), handling scope-based instance ID assignment and thread tracking.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zeroth.memory.connectors import MemoryConnector
from zeroth.memory.models import (
    ConnectorManifest,
    ConnectorScope,
    MemoryContext,
    ResolvedMemoryBinding,
)
from zeroth.runs import ThreadMemoryBinding, ThreadRepository


class InMemoryConnectorRegistry:
    """A simple lookup table that maps memory ref names to connectors.

    You register connectors by name, then look them up later when you
    need to read or write memory. Raises KeyError if a name isn't found.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[ConnectorManifest, MemoryConnector]] = {}

    def register(
        self,
        memory_ref: str,
        manifest: ConnectorManifest,
        connector: MemoryConnector,
    ) -> None:
        """Add a connector to the registry under the given name."""
        self._entries[memory_ref] = (manifest, connector)

    def resolve(self, memory_ref: str) -> tuple[ConnectorManifest, MemoryConnector]:
        """Look up a connector by name. Raises KeyError if not registered."""
        try:
            return self._entries[memory_ref]
        except KeyError as exc:
            raise KeyError(memory_ref) from exc


class MemoryConnectorResolver:
    """Turns memory ref names into fully resolved, ready-to-use bindings.

    Given a list of memory reference names, this resolver looks each one up
    in the registry, figures out the correct instance ID based on the scope
    (run, thread, or shared), builds the execution context, and optionally
    records the binding in the thread repository for tracking.
    """

    def __init__(
        self,
        *,
        registry: InMemoryConnectorRegistry | None = None,
        thread_repository: ThreadRepository | None = None,
    ) -> None:
        self.registry = registry or InMemoryConnectorRegistry()
        self.thread_repository = thread_repository

    async def resolve(
        self,
        memory_refs: list[str],
        *,
        thread_id: str | None = None,
        runtime_context: Mapping[str, Any] | None = None,
        node_id: str | None = None,
    ) -> list[ResolvedMemoryBinding]:
        """Resolve a list of memory ref names into ready-to-use bindings.

        For each ref, looks up the connector, computes the right instance ID
        based on the scope, builds a MemoryContext, and returns the complete
        binding. Also records bindings in the thread repository if available.
        """
        runtime_context = dict(runtime_context or {})
        run_id = runtime_context.get("run_id")
        bindings: list[ResolvedMemoryBinding] = []
        for memory_ref in memory_refs:
            manifest, connector = self.registry.resolve(memory_ref)
            instance_id = self._instance_id(
                manifest,
                memory_ref=memory_ref,
                run_id=run_id,
                thread_id=thread_id,
            )
            context = MemoryContext(
                memory_ref=memory_ref,
                instance_id=instance_id,
                scope=manifest.scope,
                run_id=run_id,
                thread_id=thread_id,
                node_id=node_id,
            )
            bindings.append(
                ResolvedMemoryBinding(
                    memory_ref=memory_ref,
                    manifest=manifest,
                    connector=connector,
                    context=context,
                )
            )
            await self._record_thread_binding(memory_ref, context, thread_id=thread_id)
        return bindings

    def _instance_id(
        self,
        manifest: ConnectorManifest,
        *,
        memory_ref: str,
        run_id: str | None,
        thread_id: str | None,
    ) -> str:
        """Figure out the right instance ID based on scope and available IDs.

        If the manifest has an explicit instance_id, use that. Otherwise,
        use the run_id for run-scoped connectors, the thread_id for
        thread-scoped ones, or the memory_ref name for shared connectors.
        """
        if manifest.instance_id is not None:
            return manifest.instance_id
        if manifest.scope is ConnectorScope.RUN:
            return run_id or f"{memory_ref}:run"
        if manifest.scope is ConnectorScope.THREAD:
            return thread_id or f"{memory_ref}:thread"
        return memory_ref

    async def _record_thread_binding(
        self,
        memory_ref: str,
        context: MemoryContext,
        *,
        thread_id: str | None,
    ) -> None:
        """Save the memory binding to the thread repository for tracking.

        This lets the system know which memory connectors a thread is using,
        so it can restore or clean them up later. Skips if there's no
        repository or no thread ID.
        """
        repository = self.thread_repository
        if repository is None or thread_id is None:
            return
        thread = await repository.get(thread_id)
        if thread is None:
            return
        binding = ThreadMemoryBinding(
            connector_id=memory_ref,
            instance_id=context.instance_id,
            scope=context.scope.value,
        )
        if binding in thread.memory_bindings:
            return
        thread.memory_bindings.append(binding)
        await repository.update(thread)
