"""Async database-backed persistence for immutable deployment snapshots."""

from __future__ import annotations

from datetime import datetime

from zeroth.deployments.models import Deployment, DeploymentStatus
from zeroth.deployments.provenance import (
    build_attestation_payload,
    compute_contract_snapshot_digest,
    compute_graph_snapshot_digest,
    compute_settings_snapshot_digest,
)
from zeroth.storage import AsyncDatabase
from zeroth.storage.json import load_typed_value, to_json_value


class SQLiteDeploymentRepository:
    """Persist and query deployment history using an async database."""

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    async def create(self, deployment: Deployment) -> Deployment:
        """Insert a new deployment version and supersede older active versions."""
        async with self._database.transaction() as connection:
            await connection.execute(
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
            await connection.execute(
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
        return await self.get(deployment.deployment_ref, deployment.version)  # type: ignore[return-value]

    async def get(self, deployment_ref: str, version: int | None = None) -> Deployment | None:
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
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(sql, params)
        if row is None:
            return None
        return self._row_to_deployment(row)

    async def list(self, deployment_ref: str | None = None) -> list[Deployment]:
        """Return deployment history ordered from oldest to newest."""
        sql = "SELECT * FROM deployment_versions"
        params: tuple[object, ...] = ()
        if deployment_ref is not None:
            sql += " WHERE deployment_ref = ?"
            params = (deployment_ref,)
        sql += " ORDER BY deployment_ref, version"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, params)
        return [self._row_to_deployment(row) for row in rows]

    async def next_version(self, deployment_ref: str) -> int:
        """Return the next deployment version number for a stable deployment ref."""
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(
                """
                SELECT MAX(version) AS max_version
                FROM deployment_versions
                WHERE deployment_ref = ?
                """,
                (deployment_ref,),
            )
        max_version = row["max_version"] if row is not None else None
        return int(max_version or 0) + 1

    def _row_to_deployment(self, row) -> Deployment:
        """Convert a database row to a Deployment model."""
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
