"""Database layer for approval records.

Uses SQLite to store and retrieve ApprovalRecord objects. Handles table
creation via migrations and provides simple read/write/query methods.
"""

from __future__ import annotations

from zeroth.approvals.models import ApprovalRecord, ApprovalStatus
from zeroth.storage import Migration, SQLiteDatabase
from zeroth.storage.json import load_typed_value, to_json_value

SCHEMA_SCOPE = "approvals"

MIGRATIONS = [
    Migration(
        version=1,
        name="create_approvals_table",
        sql="""
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            thread_id TEXT,
            node_id TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            record_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_approvals_run_id
            ON approvals(run_id, created_at, approval_id);
        CREATE INDEX IF NOT EXISTS idx_approvals_thread_id
            ON approvals(thread_id, created_at, approval_id);
        CREATE INDEX IF NOT EXISTS idx_approvals_deployment_ref
            ON approvals(deployment_ref, created_at, approval_id);
        """,
    )
    ,
    Migration(
        version=2,
        name="add_approval_scope_columns",
        sql="""
        ALTER TABLE approvals
        ADD COLUMN tenant_id TEXT DEFAULT 'default';

        ALTER TABLE approvals
        ADD COLUMN workspace_id TEXT;

        UPDATE approvals
        SET tenant_id = 'default'
        WHERE tenant_id IS NULL;

        CREATE INDEX IF NOT EXISTS idx_approvals_scope
            ON approvals(tenant_id, workspace_id, deployment_ref, approval_id);
        """,
    )
]


class ApprovalRepository:
    """Saves and loads approval records from a SQLite database.

    Use this when you need to persist approval requests so they survive
    restarts, or when you need to look up pending approvals by run, thread,
    or deployment.
    """

    def __init__(self, database: SQLiteDatabase):
        self._database = database
        self._database.apply_migrations(SCHEMA_SCOPE, MIGRATIONS)

    def write(self, record: ApprovalRecord) -> ApprovalRecord:
        """Save an approval record to the database.

        If a record with the same approval_id already exists, it will be
        updated. Returns the freshly-read record from the database.
        """
        with self._database.transaction() as connection:
            connection.execute(
                """
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
                    record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    record_json = excluded.record_json
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
                    to_json_value(record.model_dump(mode="json")),
                ),
            )
        return self.get(record.approval_id)

    def get(self, approval_id: str) -> ApprovalRecord | None:
        """Look up a single approval record by its ID. Returns None if not found."""
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT record_json FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        return ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))

    def list_pending(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        deployment_ref: str | None = None,
    ) -> list[ApprovalRecord]:
        """Return all approval records that are still waiting for a decision.

        You can optionally filter by run_id, thread_id, or deployment_ref.
        Results are sorted by creation time.
        """
        clauses = ["status = ?"]
        params: list[str] = [ApprovalStatus.PENDING.value]
        for key, value in (
            ("run_id", run_id),
            ("thread_id", thread_id),
            ("deployment_ref", deployment_ref),
        ):
            if value is None:
                continue
            clauses.append(f"{key} = ?")
            params.append(value)
        sql = "SELECT record_json FROM approvals WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, approval_id"
        with self._database.transaction() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]

    def list(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        deployment_ref: str | None = None,
    ) -> list[ApprovalRecord]:
        """Return approval records, optionally filtered by run, thread, or deployment."""
        clauses: list[str] = []
        params: list[str] = []
        for key, value in (
            ("run_id", run_id),
            ("thread_id", thread_id),
            ("deployment_ref", deployment_ref),
        ):
            if value is None:
                continue
            clauses.append(f"{key} = ?")
            params.append(value)
        sql = "SELECT record_json FROM approvals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, approval_id"
        with self._database.transaction() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [
            ApprovalRecord.model_validate(load_typed_value(row["record_json"], dict))
            for row in rows
        ]
