"""Async database layer for approval records.

Uses an async database to store and retrieve ApprovalRecord objects. Provides
simple read/write/query methods.
"""

from __future__ import annotations

from datetime import UTC, datetime

from zeroth.governance.approvals.models import ApprovalRecord, ApprovalStatus
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopeContext,
    ScopedTable,
    TenantWideScopeContext,
)
from zeroth.platform.storage.json import load_typed_value, to_json_value
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)

_UNSCOPED = object()


@persistence_surface("service.approvals", probe=named_isolation_probe("_drive_approvals"))
class ApprovalRepository:
    """Saves and loads approval records from an async database.

    Use this when you need to persist approval requests so they survive
    restarts, or when you need to look up pending approvals by run, thread,
    or deployment.
    """

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database
        self._sla_scope: ScopeContext | NullWorkspaceScopeContext | None = None
        self._sla_deployment_ref: str | None = None

    @classmethod
    def scoped_for_deployment(
        cls,
        database: AsyncDatabase,
        scope_context: ScopeContext | NullWorkspaceScopeContext,
        deployment_ref: str,
    ) -> ApprovalRepository:
        """Bind scheduled SLA reads to one deployment owner."""
        if type(scope_context) not in {ScopeContext, NullWorkspaceScopeContext}:
            raise TypeError("scope_context must be a trusted deployment scope")
        repository = cls(database)
        repository._sla_scope = scope_context
        repository._sla_deployment_ref = deployment_ref
        return repository

    def _approvals(
        self, tenant_id: str | None, workspace_id: str | None | object = _UNSCOPED
    ) -> ScopedTable:
        tenant = tenant_id or "default"
        if workspace_id is _UNSCOPED:
            context = (
                TenantWideScopeContext.for_default_compatibility()
                if tenant == "default"
                else TenantWideScopeContext(tenant_id=tenant)
            )
            return ScopedTable.for_privileged_tenant_wide(
                self._database,
                SERVICE_SCOPE_REGISTRY,
                "service.approvals",
                context,
            )
        context = (
            (
                NullWorkspaceScopeContext.for_default_compatibility()
                if tenant == "default"
                else NullWorkspaceScopeContext(tenant_id=tenant)
            )
            if workspace_id is None
            else (
                ScopeContext.for_default_compatibility(workspace_id=str(workspace_id))
                if tenant == "default"
                else ScopeContext(tenant_id=tenant, workspace_id=str(workspace_id))
            )
        )
        return ScopedTable(self._database, SERVICE_SCOPE_REGISTRY, "service.approvals", context)

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def write(self, record: ApprovalRecord) -> ApprovalRecord:
        """Save an approval record to the database.

        If a record with the same approval_id already exists, it will be
        updated. Returns the freshly-read record from the database.
        """
        sla_deadline_str = record.sla_deadline.isoformat() if record.sla_deadline else None
        values = {
            "approval_id": record.approval_id,
            "run_id": record.run_id,
            "thread_id": record.thread_id,
            "node_id": record.node_id,
            "graph_version_ref": record.graph_version_ref,
            "deployment_ref": record.deployment_ref,
            "status": record.status.value,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "sla_deadline": sla_deadline_str,
            "escalation_action": record.escalation_action,
            "escalated_from_id": record.escalated_from_id,
            "record_json": to_json_value(record.model_dump(mode="json")),
        }
        async with self._approvals(record.tenant_id, record.workspace_id).transaction(
            write_lock=True
        ) as approvals:
            await approvals.upsert(
                values,
                conflict_columns=("approval_id",),
                update_columns=tuple(key for key in values if key != "approval_id"),
                returning="approval_id",
                update_where={"tenant_id": record.tenant_id},
            )
        stored = await self.get(record.approval_id, tenant_id=record.tenant_id)
        if stored is None:
            # ``approval_id`` is a legacy global primary key.  A collision in
            # another tenant must not transfer ownership through the upsert.
            raise KeyError(record.approval_id)
        return stored

    @persistence_operation(ResourceOperation.READ)
    async def get(
        self,
        approval_id: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        deployment_ref: str | None = None,
        graph_version_ref: str | None = None,
    ) -> ApprovalRecord | None:
        """Look up one approval with every supplied scope predicate in SQL."""
        where: dict[str, object] = {"approval_id": approval_id}
        for field, value in (
            ("deployment_ref", deployment_ref),
            ("graph_version_ref", graph_version_ref),
        ):
            if value is not None:
                where[field] = value
        row = await self._approvals(tenant_id, workspace_id).select_one(
            where=where, columns=("record_json",)
        )
        if row is None:
            return None
        return ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def resolve_pending(self, record: ApprovalRecord) -> ApprovalRecord | None:
        """Atomically publish ``record`` only while its exact scoped row is pending."""
        async with self._approvals(record.tenant_id, record.workspace_id).transaction(
            write_lock=True
        ) as approvals:
            updated = await approvals.update_if_matches(
                {
                    "status": record.status.value,
                    "updated_at": record.updated_at.isoformat(),
                    "record_json": to_json_value(record.model_dump(mode="json")),
                },
                where={
                    "approval_id": record.approval_id,
                    "status": ApprovalStatus.PENDING.value,
                    "deployment_ref": record.deployment_ref,
                    "graph_version_ref": record.graph_version_ref,
                },
                returning="approval_id",
            )
            row = (
                await approvals.select_one(
                    where={"approval_id": record.approval_id}, columns=("record_json",)
                )
                if updated
                else None
            )
        if row is None:
            return None
        return ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_pending(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        deployment_ref: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        graph_version_ref: str | None = None,
    ) -> list[ApprovalRecord]:
        """Return all approval records that are still waiting for a decision.

        You can optionally filter by run_id, thread_id, or deployment_ref.
        Results are sorted by creation time.
        """
        where: dict[str, object] = {"status": ApprovalStatus.PENDING.value}
        for key, value in (
            ("run_id", run_id),
            ("thread_id", thread_id),
            ("deployment_ref", deployment_ref),
            ("graph_version_ref", graph_version_ref),
        ):
            if value is None:
                continue
            where[key] = value
        async with self._approvals(tenant_id, workspace_id).transaction() as approvals:
            rows = await approvals.select(
                where=where,
                columns=("record_json",),
                order_by=("created_at", "approval_id"),
            )
        return [
            ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        deployment_ref: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None | object = _UNSCOPED,
        graph_version_ref: str | None = None,
    ) -> list[ApprovalRecord]:
        """Return approval records, optionally filtered by run, thread, or deployment."""
        where: dict[str, object] = {}
        for key, value in (
            ("run_id", run_id),
            ("thread_id", thread_id),
            ("deployment_ref", deployment_ref),
            ("graph_version_ref", graph_version_ref),
        ):
            if value is None:
                continue
            where[key] = value
        async with self._approvals(tenant_id, workspace_id).transaction() as approvals:
            rows = await approvals.select(
                where=where,
                columns=("record_json",),
                order_by=("created_at", "approval_id"),
            )
        return [
            ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_overdue(self) -> list[ApprovalRecord]:
        """Return overdue approvals for this repository's deployment owner."""
        if self._sla_scope is None or self._sla_deployment_ref is None:
            raise RuntimeError("SLA reads require a deployment-bound approval repository")
        async with ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.approvals",
            self._sla_scope,
        ).transaction() as approvals:
            rows = await approvals.select(
                where={
                    "status": ApprovalStatus.PENDING.value,
                    "deployment_ref": self._sla_deployment_ref,
                },
                where_not_null=("sla_deadline",),
                where_lt={"sla_deadline": datetime.now(UTC).isoformat()},
                columns=("record_json",),
                order_by=("sla_deadline",),
            )
        return [
            ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]
