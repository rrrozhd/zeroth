"""SQLite persistence for Studio workflow metadata and draft heads."""

from __future__ import annotations

from datetime import datetime

from zeroth.storage import Migration, SQLiteDatabase
from zeroth.studio.models import WorkflowDraftHead, WorkflowRecord, WorkflowSummary

STUDIO_WORKFLOW_SCHEMA_SCOPE = "studio_workflows"
STUDIO_WORKFLOW_SCHEMA_VERSION = 1
STUDIO_WORKFLOW_MIGRATIONS = [
    Migration(
        version=1,
        name="create studio workflow metadata tables",
        sql="""
        CREATE TABLE IF NOT EXISTS workflow_records (
            workflow_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            graph_id TEXT NOT NULL,
            name TEXT NOT NULL,
            folder_path TEXT NOT NULL DEFAULT '/',
            archived_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_records_scope_folder_name
        ON workflow_records (tenant_id, workspace_id, folder_path, name);

        CREATE TABLE IF NOT EXISTS workflow_draft_heads (
            workflow_id TEXT PRIMARY KEY
                REFERENCES workflow_records(workflow_id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            draft_graph_version INTEGER NOT NULL,
            revision_token TEXT NOT NULL,
            validation_status TEXT NOT NULL,
            last_saved_at TEXT NOT NULL
        );
        """,
    )
]


class WorkflowRepository:
    """Persistence layer for workflow metadata within a workspace scope."""

    def __init__(self, database: SQLiteDatabase):
        self._database = database
        self._database.apply_migrations(STUDIO_WORKFLOW_SCHEMA_SCOPE, STUDIO_WORKFLOW_MIGRATIONS)

    @property
    def database(self) -> SQLiteDatabase:
        return self._database

    def create(self, record: WorkflowRecord, draft_head: WorkflowDraftHead) -> WorkflowRecord:
        """Insert a workflow record and its draft head in one transaction."""
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workflow_records (
                    workflow_id, tenant_id, workspace_id, graph_id, name, folder_path,
                    archived_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.workflow_id,
                    record.tenant_id,
                    record.workspace_id,
                    record.graph_id,
                    record.name,
                    record.folder_path,
                    _format_dt(record.archived_at),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO workflow_draft_heads (
                    workflow_id, tenant_id, workspace_id, draft_graph_version,
                    revision_token, validation_status, last_saved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft_head.workflow_id,
                    draft_head.tenant_id,
                    draft_head.workspace_id,
                    draft_head.draft_graph_version,
                    draft_head.revision_token,
                    draft_head.validation_status,
                    draft_head.last_saved_at.isoformat(),
                ),
            )
        return record

    def list_workflows(self, tenant_id: str, workspace_id: str) -> list[WorkflowSummary]:
        """Return workflow summaries for a specific tenant and workspace."""
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT wr.workflow_id, wr.tenant_id, wr.workspace_id, wr.graph_id, wr.name,
                       wr.folder_path, wr.updated_at, wdh.draft_graph_version,
                       wdh.validation_status, wdh.last_saved_at
                FROM workflow_records wr
                JOIN workflow_draft_heads wdh ON wdh.workflow_id = wr.workflow_id
                WHERE wr.tenant_id = ? AND wr.workspace_id = ? AND wr.archived_at IS NULL
                ORDER BY wr.folder_path, wr.name
                """,
                (tenant_id, workspace_id),
            ).fetchall()
        return [self._build_summary(row) for row in rows]

    def get_workflow(
        self,
        tenant_id: str,
        workspace_id: str,
        workflow_id: str,
    ) -> tuple[WorkflowRecord, WorkflowDraftHead] | None:
        """Load a workflow record and draft head only from its owning scope."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT wr.workflow_id, wr.tenant_id, wr.workspace_id, wr.graph_id, wr.name,
                       wr.folder_path, wr.archived_at, wr.created_at, wr.updated_at,
                       wdh.draft_graph_version, wdh.revision_token, wdh.validation_status,
                       wdh.last_saved_at
                FROM workflow_records wr
                JOIN workflow_draft_heads wdh ON wdh.workflow_id = wr.workflow_id
                WHERE wr.workflow_id = ?
                  AND wr.tenant_id = ?
                  AND wr.workspace_id = ?
                  AND wr.archived_at IS NULL
                """,
                (workflow_id, tenant_id, workspace_id),
            ).fetchone()
        if row is None:
            return None
        return self._build_record(row), self._build_draft_head(row)

    def has_workflow(self, workflow_id: str, tenant_id: str, workspace_id: str) -> bool:
        """Return whether the workflow exists inside the requested scope."""
        return self.get_workflow(tenant_id, workspace_id, workflow_id) is not None

    def update_draft(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        workspace_id: str,
        draft_graph_version: int,
        revision_token: str,
        validation_status: str,
        last_saved_at: datetime,
        updated_at: datetime,
    ) -> None:
        """Update the mutable draft pointer and workflow timestamp inside one scope."""
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE workflow_records
                SET updated_at = ?
                WHERE workflow_id = ? AND tenant_id = ? AND workspace_id = ?
                """,
                (updated_at.isoformat(), workflow_id, tenant_id, workspace_id),
            )
            connection.execute(
                """
                UPDATE workflow_draft_heads
                SET draft_graph_version = ?, revision_token = ?, validation_status = ?,
                    last_saved_at = ?
                WHERE workflow_id = ? AND tenant_id = ? AND workspace_id = ?
                """,
                (
                    draft_graph_version,
                    revision_token,
                    validation_status,
                    last_saved_at.isoformat(),
                    workflow_id,
                    tenant_id,
                    workspace_id,
                ),
            )

    def _build_summary(self, row) -> WorkflowSummary:
        return WorkflowSummary(
            workflow_id=row["workflow_id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            graph_id=row["graph_id"],
            name=row["name"],
            folder_path=row["folder_path"],
            draft_graph_version=row["draft_graph_version"],
            validation_status=row["validation_status"],
            updated_at=_parse_dt(row["updated_at"]),
            last_saved_at=_parse_dt(row["last_saved_at"]),
        )

    def _build_record(self, row) -> WorkflowRecord:
        return WorkflowRecord(
            workflow_id=row["workflow_id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            graph_id=row["graph_id"],
            name=row["name"],
            folder_path=row["folder_path"],
            archived_at=_parse_optional_dt(row["archived_at"]),
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )

    def _build_draft_head(self, row) -> WorkflowDraftHead:
        return WorkflowDraftHead(
            workflow_id=row["workflow_id"],
            tenant_id=row["tenant_id"],
            workspace_id=row["workspace_id"],
            draft_graph_version=row["draft_graph_version"],
            revision_token=row["revision_token"],
            validation_status=row["validation_status"],
            last_saved_at=_parse_dt(row["last_saved_at"]),
        )


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_optional_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)
