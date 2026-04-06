"""Async database-backed storage for audit records.

Provides the AuditRepository class that handles saving and querying
NodeAuditRecord objects using an async database.
"""

from __future__ import annotations

from collections.abc import Sequence

from zeroth.audit.models import AuditQuery, NodeAuditRecord
from zeroth.audit.verifier import compute_chained_record
from zeroth.storage import AsyncDatabase
from zeroth.storage.json import load_typed_value, to_json_value


class AuditRepository:
    """Saves and retrieves audit records from an async database.

    Use this class to store audit records when nodes run and to look them
    up later for debugging, compliance, or building timelines.
    """

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    async def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Save an audit record to the database.

        Writes are append-only. Duplicate audit IDs are rejected so history
        cannot be silently rewritten.
        """
        async with self._database.transaction() as connection:
            previous = await self._latest_for_run(connection, record.run_id)
            chained = compute_chained_record(
                record,
                previous.record_digest if previous is not None else None,
            )
            existing = await connection.fetch_one(
                "SELECT 1 FROM node_audits WHERE audit_id = ?",
                (chained.audit_id,),
            )
            if existing is not None:
                raise ValueError(f"audit_id {record.audit_id!r} already exists")
            await connection.execute(
                """
                INSERT INTO node_audits (
                    audit_id,
                    run_id,
                    thread_id,
                    node_id,
                    graph_version_ref,
                    deployment_ref,
                    tenant_id,
                    workspace_id,
                    created_at,
                    record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chained.audit_id,
                    chained.run_id,
                    chained.thread_id,
                    chained.node_id,
                    chained.graph_version_ref,
                    chained.deployment_ref,
                    chained.tenant_id,
                    chained.workspace_id,
                    chained.started_at.isoformat(),
                    to_json_value(chained.model_dump(mode="json")),
                ),
            )
        return await self.get(record.audit_id)

    async def get(self, audit_id: str) -> NodeAuditRecord | None:
        """Look up a single audit record by its ID. Returns None if not found."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                "SELECT record_json FROM node_audits WHERE audit_id = ?",
                (audit_id,),
            )
        if row is None:
            return None
        return NodeAuditRecord.model_validate(load_typed_value(row["record_json"], dict))

    async def list(self, query: AuditQuery | None = None) -> list[NodeAuditRecord]:
        """Return audit records matching the given filters, ordered by time.

        Pass an AuditQuery to filter by run, thread, node, etc. If no query
        is given, all records are returned.
        """
        query = query or AuditQuery()
        clauses: list[str] = []
        params: list[str] = []
        for field in ("run_id", "thread_id", "node_id", "graph_version_ref", "deployment_ref"):
            value = getattr(query, field)
            if value is None:
                continue
            clauses.append(f"{field} = ?")
            params.append(value)
        sql = "SELECT record_json FROM node_audits"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, audit_id"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, tuple(params))
        return [
            NodeAuditRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]

    async def list_by_run(self, run_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific run."""
        return await self.list(AuditQuery(run_id=run_id))

    async def list_by_thread(self, thread_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific thread."""
        return await self.list(AuditQuery(thread_id=thread_id))

    async def list_by_node(self, node_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific node."""
        return await self.list(AuditQuery(node_id=node_id))

    async def list_by_graph_version(self, graph_version_ref: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific graph version."""
        return await self.list(AuditQuery(graph_version_ref=graph_version_ref))

    async def list_by_deployment(self, deployment_ref: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific deployment."""
        return await self.list(AuditQuery(deployment_ref=deployment_ref))

    async def write_many(self, records: Sequence[NodeAuditRecord]) -> list[NodeAuditRecord]:
        """Save multiple audit records at once. Returns all saved records."""
        return [await self.write(record) for record in records]

    async def _latest_for_run(self, connection, run_id: str) -> NodeAuditRecord | None:  # noqa: ANN001
        row = await connection.fetch_one(
            """
            SELECT record_json
            FROM node_audits
            WHERE run_id = ?
            ORDER BY created_at DESC, audit_id DESC
            LIMIT 1
            """,
            (run_id,),
        )
        if row is None:
            return None
        return NodeAuditRecord.model_validate(load_typed_value(row["record_json"], dict))
