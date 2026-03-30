"""SQLite-backed storage for audit records.

Provides the AuditRepository class that handles saving and querying
NodeAuditRecord objects in a SQLite database. The database schema is
set up automatically via migrations when the repository is created.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from zeroth.audit.models import AuditQuery, NodeAuditRecord
from zeroth.audit.verifier import compute_chained_record
from zeroth.storage import Migration, SQLiteDatabase
from zeroth.storage.json import load_typed_value, to_json_value

SCHEMA_SCOPE = "audit"

MIGRATIONS = [
    Migration(
        version=1,
        name="create_node_audits_table",
        sql="""
        CREATE TABLE IF NOT EXISTS node_audits (
            audit_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            thread_id TEXT,
            node_id TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            created_at TEXT NOT NULL,
            record_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_node_audits_run_id
            ON node_audits(run_id, created_at, audit_id);
        CREATE INDEX IF NOT EXISTS idx_node_audits_thread_id
            ON node_audits(thread_id, created_at, audit_id);
        CREATE INDEX IF NOT EXISTS idx_node_audits_node_id
            ON node_audits(node_id, created_at, audit_id);
        CREATE INDEX IF NOT EXISTS idx_node_audits_graph_version_ref
            ON node_audits(graph_version_ref, created_at, audit_id);
        CREATE INDEX IF NOT EXISTS idx_node_audits_deployment_ref
            ON node_audits(deployment_ref, created_at, audit_id);
        """,
    )
    ,
    Migration(
        version=2,
        name="add_audit_scope_columns",
        sql="""
        ALTER TABLE node_audits
        ADD COLUMN tenant_id TEXT DEFAULT 'default';

        ALTER TABLE node_audits
        ADD COLUMN workspace_id TEXT;

        UPDATE node_audits
        SET tenant_id = 'default'
        WHERE tenant_id IS NULL;

        CREATE INDEX IF NOT EXISTS idx_node_audits_scope
            ON node_audits(tenant_id, workspace_id, deployment_ref, created_at, audit_id);
        """,
    )
]


class AuditRepository:
    """Saves and retrieves audit records from a SQLite database.

    Use this class to store audit records when nodes run and to look them
    up later for debugging, compliance, or building timelines.
    """

    def __init__(self, database: SQLiteDatabase):
        self._database = database
        self._database.apply_migrations(SCHEMA_SCOPE, MIGRATIONS)

    def write(self, record: NodeAuditRecord) -> NodeAuditRecord:
        """Save an audit record to the database.

        Writes are append-only. Duplicate audit IDs are rejected so history
        cannot be silently rewritten.
        """
        with self._database.transaction() as connection:
            previous = self._latest_for_run(connection, record.run_id)
            chained = compute_chained_record(
                record,
                previous.record_digest if previous is not None else None,
            )
            try:
                connection.execute(
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
                        self._encode_record_json(to_json_value(chained.model_dump(mode="json"))),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"audit_id {record.audit_id!r} already exists") from exc
        return self.get(record.audit_id)

    def get(self, audit_id: str) -> NodeAuditRecord | None:
        """Look up a single audit record by its ID. Returns None if not found."""
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM node_audits WHERE audit_id = ?",
                (audit_id,),
            ).fetchone()
        if row is None:
            return None
        return NodeAuditRecord.model_validate(
            load_typed_value(self._decode_record_json(row["record_json"]), dict)
        )

    def list(self, query: AuditQuery | None = None) -> list[NodeAuditRecord]:
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
        with self._database.transaction() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            NodeAuditRecord.model_validate(
                load_typed_value(self._decode_record_json(row["record_json"]), dict)
            )
            for row in rows
        ]

    def list_by_run(self, run_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific run."""
        return self.list(AuditQuery(run_id=run_id))

    def list_by_thread(self, thread_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific thread."""
        return self.list(AuditQuery(thread_id=thread_id))

    def list_by_node(self, node_id: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific node."""
        return self.list(AuditQuery(node_id=node_id))

    def list_by_graph_version(self, graph_version_ref: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific graph version."""
        return self.list(AuditQuery(graph_version_ref=graph_version_ref))

    def list_by_deployment(self, deployment_ref: str) -> list[NodeAuditRecord]:
        """Return all audit records for a specific deployment."""
        return self.list(AuditQuery(deployment_ref=deployment_ref))

    def write_many(self, records: Sequence[NodeAuditRecord]) -> list[NodeAuditRecord]:
        """Save multiple audit records at once. Returns all saved records."""
        return [self.write(record) for record in records]

    def _latest_for_run(self, connection, run_id: str) -> NodeAuditRecord | None:  # noqa: ANN001
        row = connection.execute(
            """
            SELECT record_json
            FROM node_audits
            WHERE run_id = ?
            ORDER BY created_at DESC, audit_id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return NodeAuditRecord.model_validate(
            load_typed_value(self._decode_record_json(row["record_json"]), dict)
        )

    def _encode_record_json(self, payload: str) -> str:
        encrypted_field = self._database.encrypted_field
        if encrypted_field is None:
            return payload
        return encrypted_field.encrypt(payload)

    def _decode_record_json(self, payload: str) -> str:
        encrypted_field = self._database.encrypted_field
        if encrypted_field is None:
            return payload
        return encrypted_field.decrypt(payload)
