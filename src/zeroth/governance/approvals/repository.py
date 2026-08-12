"""Async database layer for approval records.

Uses an async database to store and retrieve ApprovalRecord objects. Provides
simple read/write/query methods.
"""

from __future__ import annotations

from datetime import UTC, datetime

from zeroth.governance.approvals.models import ApprovalRecord, ApprovalStatus
from zeroth.platform.storage import AsyncDatabase, ResourceOperation
from zeroth.platform.storage.json import load_typed_value, to_json_value
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)

_UNSCOPED = object()


def _tenant_predicate(tenant_id: str | None) -> tuple[str | None, tuple[str, ...]]:
    """Render the tenant predicate shared by scoped approval operations."""
    if tenant_id is None:
        return None, ()
    return "tenant_id = ?", (tenant_id,)


def _ownership_conflict_clause() -> str:
    """Keep a legacy global approval ID from transferring tenant ownership."""
    return "WHERE approvals.tenant_id = excluded.tenant_id"


@persistence_surface("service.approvals", probe=named_isolation_probe("_drive_approvals"))
class ApprovalRepository:
    """Saves and loads approval records from an async database.

    Use this when you need to persist approval requests so they survive
    restarts, or when you need to look up pending approvals by run, thread,
    or deployment.
    """

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def write(self, record: ApprovalRecord) -> ApprovalRecord:
        """Save an approval record to the database.

        If a record with the same approval_id already exists, it will be
        updated. Returns the freshly-read record from the database.
        """
        sla_deadline_str = record.sla_deadline.isoformat() if record.sla_deadline else None
        async with self._database.transaction() as connection:
            await connection.execute(
                f"""
                INSERT INTO approvals (
                    approval_id,
                    run_id,
                    thread_id,
                    node_id,
                    graph_version_ref,
                    deployment_ref,
                    tenant_id,
                    workspace_id,
                    status,
                    created_at,
                    updated_at,
                    sla_deadline,
                    escalation_action,
                    escalated_from_id,
                    record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    thread_id = excluded.thread_id,
                    node_id = excluded.node_id,
                    graph_version_ref = excluded.graph_version_ref,
                    deployment_ref = excluded.deployment_ref,
                    tenant_id = excluded.tenant_id,
                    workspace_id = excluded.workspace_id,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    sla_deadline = excluded.sla_deadline,
                    escalation_action = excluded.escalation_action,
                    escalated_from_id = excluded.escalated_from_id,
                    record_json = excluded.record_json
                {_ownership_conflict_clause()}
                """,
                (
                    record.approval_id,
                    record.run_id,
                    record.thread_id,
                    record.node_id,
                    record.graph_version_ref,
                    record.deployment_ref,
                    record.tenant_id,
                    record.workspace_id,
                    record.status.value,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    sla_deadline_str,
                    record.escalation_action,
                    record.escalated_from_id,
                    to_json_value(record.model_dump(mode="json")),
                ),
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
        sql = "SELECT record_json FROM approvals WHERE approval_id = ?"
        params: list[str] = [approval_id]
        tenant_sql, tenant_params = _tenant_predicate(tenant_id)
        if tenant_sql is not None:
            sql += f" AND {tenant_sql}"
            params.extend(tenant_params)
        for field, value in (
            ("deployment_ref", deployment_ref),
            ("graph_version_ref", graph_version_ref),
        ):
            if value is not None:
                sql += f" AND {field} = ?"
                params.append(value)
        if workspace_id is not _UNSCOPED:
            if workspace_id is None:
                sql += " AND workspace_id IS NULL"
            else:
                sql += " AND workspace_id = ?"
                params.append(str(workspace_id))
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(sql, tuple(params))
        if row is None:
            return None
        return ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))

    @persistence_operation(ResourceOperation.READ, ResourceOperation.UPDATE)
    async def resolve_pending(self, record: ApprovalRecord) -> ApprovalRecord | None:
        """Atomically publish ``record`` only while its exact scoped row is pending."""
        sql = """UPDATE approvals
                 SET status = ?, updated_at = ?, record_json = ?
                 WHERE approval_id = ? AND status = ?
                   AND deployment_ref = ? AND graph_version_ref = ?"""
        params: list[object] = [
            record.status.value,
            record.updated_at.isoformat(),
            to_json_value(record.model_dump(mode="json")),
            record.approval_id,
            ApprovalStatus.PENDING.value,
            record.deployment_ref,
            record.graph_version_ref,
        ]
        tenant_sql, tenant_params = _tenant_predicate(record.tenant_id)
        assert tenant_sql is not None
        sql += f" AND {tenant_sql}"
        params.extend(tenant_params)
        if record.workspace_id is None:
            sql += " AND workspace_id IS NULL"
        else:
            sql += " AND workspace_id = ?"
            params.append(record.workspace_id)
        sql += " RETURNING record_json"
        async with self._database.transaction(write_lock=True) as connection:
            row = await connection.fetch_one(sql, tuple(params))
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
        clauses = ["status = ?"]
        params: list[str] = [ApprovalStatus.PENDING.value]
        tenant_sql, tenant_params = _tenant_predicate(tenant_id)
        if tenant_sql is not None:
            clauses.append(tenant_sql)
            params.extend(tenant_params)
        for key, value in (
            ("run_id", run_id),
            ("thread_id", thread_id),
            ("deployment_ref", deployment_ref),
            ("graph_version_ref", graph_version_ref),
        ):
            if value is None:
                continue
            clauses.append(f"{key} = ?")
            params.append(value)
        if workspace_id is not _UNSCOPED:
            if workspace_id is None:
                clauses.append("workspace_id IS NULL")
            else:
                clauses.append("workspace_id = ?")
                params.append(str(workspace_id))
        sql = "SELECT record_json FROM approvals WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, approval_id"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, tuple(params))
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
        clauses: list[str] = []
        params: list[str] = []
        tenant_sql, tenant_params = _tenant_predicate(tenant_id)
        if tenant_sql is not None:
            clauses.append(tenant_sql)
            params.extend(tenant_params)
        for key, value in (
            ("run_id", run_id),
            ("thread_id", thread_id),
            ("deployment_ref", deployment_ref),
            ("graph_version_ref", graph_version_ref),
        ):
            if value is None:
                continue
            clauses.append(f"{key} = ?")
            params.append(value)
        if workspace_id is not _UNSCOPED:
            if workspace_id is None:
                clauses.append("workspace_id IS NULL")
            else:
                clauses.append("workspace_id = ?")
                params.append(str(workspace_id))
        sql = "SELECT record_json FROM approvals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, approval_id"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, tuple(params))
        return [
            ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_overdue(self) -> list[ApprovalRecord]:
        """Return PENDING approvals past their SLA deadline."""
        now = datetime.now(UTC).isoformat()
        sql = (
            "SELECT record_json FROM approvals "
            "WHERE status = ? AND sla_deadline IS NOT NULL AND sla_deadline < ? "
            "ORDER BY sla_deadline"
        )
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, (ApprovalStatus.PENDING.value, now))
        return [
            ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]
