"""SQLite persistence for Studio workflow edit leases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from zeroth.storage import Migration, SQLiteDatabase
from zeroth.studio.models import WorkflowLease, WorkflowLeaseConflict

STUDIO_WORKFLOW_LEASE_SCOPE = "studio_workflow_leases"
STUDIO_WORKFLOW_LEASE_MIGRATIONS = [
    Migration(
        version=1,
        name="create studio workflow lease table",
        sql="""
        CREATE TABLE IF NOT EXISTS workflow_leases (
            workflow_id TEXT PRIMARY KEY
                REFERENCES workflow_records(workflow_id) ON DELETE CASCADE,
            tenant_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL,
            lease_token TEXT NOT NULL,
            subject TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        """,
    )
]


class WorkflowLeaseRepository:
    """Persistence layer for workspace-scoped workflow edit leases."""

    def __init__(self, database: SQLiteDatabase):
        self._database = database
        self._database.apply_migrations(
            STUDIO_WORKFLOW_LEASE_SCOPE,
            STUDIO_WORKFLOW_LEASE_MIGRATIONS,
        )

    def acquire_lease(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        workspace_id: str,
        subject: str,
        ttl_seconds: int = 30,
    ) -> WorkflowLease | WorkflowLeaseConflict:
        """Acquire or replace an expired lease for a scoped workflow."""
        now = _utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT workflow_id, tenant_id, workspace_id, lease_token, subject,
                       acquired_at, expires_at
                FROM workflow_leases
                WHERE workflow_id = ? AND tenant_id = ? AND workspace_id = ?
                """,
                (workflow_id, tenant_id, workspace_id),
            ).fetchone()
            if row is not None and _parse_dt(row["expires_at"]) > now:
                return WorkflowLeaseConflict(
                    workflow_id=row["workflow_id"],
                    tenant_id=row["tenant_id"],
                    workspace_id=row["workspace_id"],
                    lease_token=row["lease_token"],
                    subject=row["subject"],
                    expires_at=_parse_dt(row["expires_at"]),
                )
            lease = WorkflowLease(
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                lease_token=uuid4().hex,
                subject=subject,
                acquired_at=now,
                expires_at=expires_at,
            )
            if row is None:
                connection.execute(
                    """
                    INSERT INTO workflow_leases (
                        workflow_id, tenant_id, workspace_id, lease_token,
                        subject, acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease.workflow_id,
                        lease.tenant_id,
                        lease.workspace_id,
                        lease.lease_token,
                        lease.subject,
                        lease.acquired_at.isoformat(),
                        lease.expires_at.isoformat(),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE workflow_leases
                    SET lease_token = ?, subject = ?, acquired_at = ?, expires_at = ?
                    WHERE workflow_id = ? AND tenant_id = ? AND workspace_id = ?
                    """,
                    (
                        lease.lease_token,
                        lease.subject,
                        lease.acquired_at.isoformat(),
                        lease.expires_at.isoformat(),
                        workflow_id,
                        tenant_id,
                        workspace_id,
                    ),
                )
        return lease

    def renew_lease(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        workspace_id: str,
        lease_token: str,
        ttl_seconds: int = 30,
    ) -> WorkflowLease | None:
        """Extend an active lease if the workflow scope and token still match."""
        now = _utc_now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT workflow_id, tenant_id, workspace_id, lease_token, subject,
                       acquired_at, expires_at
                FROM workflow_leases
                WHERE workflow_id = ? AND tenant_id = ? AND workspace_id = ?
                  AND lease_token = ?
                """,
                (workflow_id, tenant_id, workspace_id, lease_token),
            ).fetchone()
            if row is None or _parse_dt(row["expires_at"]) <= now:
                return None
            connection.execute(
                """
                UPDATE workflow_leases
                SET expires_at = ?
                WHERE workflow_id = ? AND tenant_id = ? AND workspace_id = ?
                  AND lease_token = ?
                """,
                (expires_at.isoformat(), workflow_id, tenant_id, workspace_id, lease_token),
            )
        return WorkflowLease(
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            lease_token=lease_token,
            subject=row["subject"],
            acquired_at=_parse_dt(row["acquired_at"]),
            expires_at=expires_at,
        )

    def release_lease(
        self,
        *,
        workflow_id: str,
        tenant_id: str,
        workspace_id: str,
        lease_token: str,
    ) -> bool:
        """Delete a lease only when both the scope and token match."""
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM workflow_leases
                WHERE workflow_id = ? AND tenant_id = ? AND workspace_id = ?
                  AND lease_token = ?
                """,
                (workflow_id, tenant_id, workspace_id, lease_token),
            )
        return cursor.rowcount > 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)
