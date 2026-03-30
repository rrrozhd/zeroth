"""SQLite-backed persistence for immutable deployment snapshots."""

from __future__ import annotations

from datetime import datetime

from zeroth.deployments.models import Deployment, DeploymentStatus
from zeroth.deployments.provenance import (
    build_attestation_payload,
    compute_contract_snapshot_digest,
    compute_graph_snapshot_digest,
    compute_settings_snapshot_digest,
)
from zeroth.storage import Migration, SQLiteDatabase
from zeroth.storage.json import load_typed_value, to_json_value

SCHEMA_SCOPE = "deployments"
SCHEMA_VERSION = 4

MIGRATIONS = (
    Migration(
        version=1,
        name="create_deployment_versions",
        sql="""
        CREATE TABLE IF NOT EXISTS deployment_versions (
            deployment_id TEXT PRIMARY KEY,
            deployment_ref TEXT NOT NULL,
            version INTEGER NOT NULL,
            graph_id TEXT NOT NULL,
            graph_version INTEGER NOT NULL,
            graph_version_ref TEXT NOT NULL,
            serialized_graph TEXT NOT NULL,
            entry_input_contract_ref TEXT,
            entry_output_contract_ref TEXT,
            deployment_settings_snapshot TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(deployment_ref, version)
        );

        CREATE INDEX IF NOT EXISTS idx_deployment_versions_ref_version
            ON deployment_versions(deployment_ref, version DESC);
        CREATE INDEX IF NOT EXISTS idx_deployment_versions_graph
            ON deployment_versions(graph_id, graph_version, created_at, deployment_id);
        """,
    ),
    Migration(
        version=2,
        name="add_entry_contract_versions",
        sql="""
        ALTER TABLE deployment_versions
        ADD COLUMN entry_input_contract_version INTEGER;

        ALTER TABLE deployment_versions
        ADD COLUMN entry_output_contract_version INTEGER;
        """,
    ),
    Migration(
        version=3,
        name="add_deployment_scope_columns",
        sql="""
        ALTER TABLE deployment_versions
        ADD COLUMN tenant_id TEXT DEFAULT 'default';

        ALTER TABLE deployment_versions
        ADD COLUMN workspace_id TEXT;

        UPDATE deployment_versions
        SET tenant_id = 'default'
        WHERE tenant_id IS NULL;

        CREATE INDEX IF NOT EXISTS idx_deployment_versions_scope
            ON deployment_versions(tenant_id, workspace_id, deployment_ref, version DESC);
        """,
    ),
    Migration(
        version=4,
        name="add_provenance_digest_columns",
        sql="""
        ALTER TABLE deployment_versions
        ADD COLUMN graph_snapshot_digest TEXT;

        ALTER TABLE deployment_versions
        ADD COLUMN contract_snapshot_digest TEXT;

        ALTER TABLE deployment_versions
        ADD COLUMN settings_snapshot_digest TEXT;

        ALTER TABLE deployment_versions
        ADD COLUMN attestation_digest TEXT;
        """,
    ),
)


class SQLiteDeploymentRepository:
    """Persist and query deployment history in SQLite."""

    def __init__(self, database: SQLiteDatabase):
        self._database = database
        self._database.apply_migrations(SCHEMA_SCOPE, MIGRATIONS)

    def create(self, deployment: Deployment) -> Deployment:
        """Insert a new deployment version and supersede older active versions."""
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE deployment_versions
                SET status = ?, updated_at = ?
                WHERE deployment_ref = ? AND status = ?
                """,
                (
                    DeploymentStatus.SUPERSEDED.value,
                    deployment.updated_at.isoformat(),
                    deployment.deployment_ref,
                    DeploymentStatus.ACTIVE.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO deployment_versions (
                    deployment_id,
                    deployment_ref,
                    version,
                    graph_id,
                    graph_version,
                    graph_version_ref,
                    serialized_graph,
                    entry_input_contract_ref,
                    entry_input_contract_version,
                    entry_output_contract_ref,
                    entry_output_contract_version,
                    deployment_settings_snapshot,
                    graph_snapshot_digest,
                    contract_snapshot_digest,
                    settings_snapshot_digest,
                    attestation_digest,
                    tenant_id,
                    workspace_id,
                    status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    deployment.deployment_id,
                    deployment.deployment_ref,
                    deployment.version,
                    deployment.graph_id,
                    deployment.graph_version,
                    deployment.graph_version_ref,
                    deployment.serialized_graph,
                    deployment.entry_input_contract_ref,
                    deployment.entry_input_contract_version,
                    deployment.entry_output_contract_ref,
                    deployment.entry_output_contract_version,
                    to_json_value(deployment.deployment_settings_snapshot),
                    deployment.graph_snapshot_digest,
                    deployment.contract_snapshot_digest,
                    deployment.settings_snapshot_digest,
                    deployment.attestation_digest,
                    deployment.tenant_id,
                    deployment.workspace_id,
                    deployment.status.value,
                    deployment.created_at.isoformat(),
                    deployment.updated_at.isoformat(),
                ),
            )
        return self.get(deployment.deployment_ref, deployment.version)  # type: ignore[return-value]

    def get(self, deployment_ref: str, version: int | None = None) -> Deployment | None:
        """Load the latest or a specific deployment version."""
        sql = """
            SELECT *
            FROM deployment_versions
            WHERE deployment_ref = ?
        """
        params: tuple[object, ...]
        if version is not None:
            sql += " AND version = ?"
            params = (deployment_ref, version)
        else:
            params = (deployment_ref,)
        sql += " ORDER BY version DESC LIMIT 1"
        with self._database.transaction() as connection:
            row = connection.execute(sql, params).fetchone()
        if row is None:
            return None
        return self._row_to_deployment(row)

    def list(self, deployment_ref: str | None = None) -> list[Deployment]:
        """Return deployment history ordered from oldest to newest."""
        sql = "SELECT * FROM deployment_versions"
        params: tuple[object, ...] = ()
        if deployment_ref is not None:
            sql += " WHERE deployment_ref = ?"
            params = (deployment_ref,)
        sql += " ORDER BY deployment_ref, version"
        with self._database.transaction() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_deployment(row) for row in rows]

    def next_version(self, deployment_ref: str) -> int:
        """Return the next deployment version number for a stable deployment ref."""
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT MAX(version) AS max_version
                FROM deployment_versions
                WHERE deployment_ref = ?
                """,
                (deployment_ref,),
            ).fetchone()
        max_version = row["max_version"] if row is not None else None
        return int(max_version or 0) + 1

    def _row_to_deployment(self, row) -> Deployment:
        """Convert a SQLite row to a Deployment model."""
        settings_snapshot = load_typed_value(
            row["deployment_settings_snapshot"],
            dict,
        )
        graph_snapshot_digest = row["graph_snapshot_digest"] or compute_graph_snapshot_digest(
            row["serialized_graph"]
        )
        contract_snapshot_digest = row["contract_snapshot_digest"] or (
            compute_contract_snapshot_digest(
                entry_input_contract_ref=row["entry_input_contract_ref"],
                entry_input_contract_version=row["entry_input_contract_version"],
                entry_output_contract_ref=row["entry_output_contract_ref"],
                entry_output_contract_version=row["entry_output_contract_version"],
            )
        )
        settings_snapshot_digest = row["settings_snapshot_digest"] or (
            compute_settings_snapshot_digest(settings_snapshot)
        )
        deployment = Deployment(
            deployment_id=row["deployment_id"],
            deployment_ref=row["deployment_ref"],
            version=row["version"],
            graph_id=row["graph_id"],
            graph_version=row["graph_version"],
            graph_version_ref=row["graph_version_ref"],
            serialized_graph=row["serialized_graph"],
            entry_input_contract_ref=row["entry_input_contract_ref"],
            entry_input_contract_version=row["entry_input_contract_version"],
            entry_output_contract_ref=row["entry_output_contract_ref"],
            entry_output_contract_version=row["entry_output_contract_version"],
            deployment_settings_snapshot=settings_snapshot,
            graph_snapshot_digest=graph_snapshot_digest,
            contract_snapshot_digest=contract_snapshot_digest,
            settings_snapshot_digest=settings_snapshot_digest,
            attestation_digest=row["attestation_digest"] or "",
            tenant_id=row["tenant_id"] or "default",
            workspace_id=row["workspace_id"],
            status=DeploymentStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
        if not deployment.attestation_digest:
            deployment.attestation_digest = str(
                build_attestation_payload(deployment)["attestation_digest"]
            )
        return deployment
