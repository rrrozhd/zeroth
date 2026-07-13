"""Registry and resolver for memory connectors.

The registry stores known connectors by name. The resolver takes a list of
memory reference names and turns them into ready-to-use bindings (connector
wrapped with ScopedMemoryConnector and optionally AuditingMemoryConnector).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zeroth.core.governed.audit.emitter import AuditEmitter
from zeroth.core.governed.memory.auditing import AuditingMemoryConnector
from zeroth.core.governed.memory.models import MemoryScope
from zeroth.core.governed.memory.scoped import ScopedMemoryConnector
from zeroth.core.memory.capability_guard import CapabilityEnforcingMemoryConnector
from zeroth.core.memory.models import (
    ConnectorManifest,
    ResolvedMemoryBinding,
)
from zeroth.core.memory.tenant_scoped import TenantScopedMemoryConnector
from zeroth.core.policy.models import Capability
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

    def unregister(self, memory_ref: str) -> None:
        """Remove a connector from the registry. Missing refs are a no-op."""
        self._entries.pop(memory_ref, None)

    def resolve(self, memory_ref: str) -> tuple[ConnectorManifest, Any]:
        """Look up a connector by name. Raises KeyError if not registered."""
        try:
            return self._entries[memory_ref]
        except KeyError as exc:
            raise KeyError(memory_ref) from exc

    def list(self) -> dict[str, tuple[ConnectorManifest, Any]]:
        """All registered entries by ref (shallow copy; used by /v1/connectors)."""
        return dict(self._entries)


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
        effective_capabilities: set[Capability] | None = None,
    ) -> list[ResolvedMemoryBinding]:
        """Resolve a list of memory ref names into ready-to-use bindings.

        For each ref, looks up the connector and builds the wrapper stack
        ``Scoped(TenantScoped(Auditing(raw)))``: Auditing (innermost, optional)
        emits events, TenantScoped rewrites the resolved target into a
        tenant-namespaced form, and Scoped (outermost) resolves ``scope ->
        target`` before either sees it. TenantScoped sits **below** Scoped on
        purpose: Scoped first produces ``"__shared__"`` / run_id / thread_id,
        then TenantScoped namespaces it — that is what stops SHARED memory from
        being cross-tenant readable on a shared backend.

        Tenant is read per-call from ``runtime_context["tenant_id"]`` and is
        fail-closed: an empty/missing tenant raises ``TenantScopeError`` (an
        explicit ``"default"`` sentinel is permitted). The resolver stays a
        shared singleton, so tenant must never be stored on ``__init__``.

        ``effective_capabilities`` (WS-C) is the node's granted capability set.
        When it is not None the connector is wrapped with
        ``CapabilityEnforcingMemoryConnector`` as the OUTERMOST layer, so
        ``MEMORY_READ`` / ``MEMORY_WRITE`` are enforced (fail-closed: an empty
        granted set denies) before scope/tenant/audit/raw see the call. When it
        is None enforcement is inactive (the policy guard is not wired) and the
        stack is left unchanged — the caller, not this method, decides whether
        enforcement applies, so None never silently bypasses an active gate.
        """
        runtime_context = dict(runtime_context or {})
        run_id = runtime_context.get("run_id", "unknown")
        tenant_id = runtime_context.get("tenant_id")
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

            # Namespace by tenant BELOW Scoped so the already-resolved target
            # (incl. the SHARED "__shared__" literal) is rewritten before the
            # raw backend keys on it. Fail-closed on empty/missing tenant.
            wrapped = TenantScopedMemoryConnector(wrapped, tenant_id=tenant_id)

            # Wrap with ScopedMemoryConnector for automatic target resolution
            wrapped = ScopedMemoryConnector(
                wrapped,
                run_id=run_id,
                thread_id=thread_id,
                workflow_name=self._workflow_name,
            )

            # WS-C: capability gate is the OUTERMOST layer, so a denied read/write
            # never reaches scope/tenant/audit/raw. Only applied when enforcement
            # is active (effective_capabilities is not None); fail-closed within.
            if effective_capabilities is not None:
                wrapped = CapabilityEnforcingMemoryConnector(
                    wrapped,
                    effective_capabilities=effective_capabilities,
                    node_id=node_id or run_id,
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
