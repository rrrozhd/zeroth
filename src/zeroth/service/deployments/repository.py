"""Async database-backed persistence for immutable deployment snapshots."""

from __future__ import annotations

from datetime import datetime

from zeroth.platform.storage import AsyncDatabase
from zeroth.platform.storage.json import load_typed_value, to_json_value
from zeroth.service.deployments.models import (
    Deployment,
    DeploymentEngineMode,
    DeploymentStatus,
)
from zeroth.service.deployments.provenance import (
    build_attestation_payload,
    compute_contract_snapshot_digest,
    compute_graph_snapshot_digest,
    compute_settings_snapshot_digest,
)


class DeploymentRefLineageConflictError(RuntimeError):
    """A deployment ref was already bound to another graph lineage."""

    def __init__(self, deployment_ref: str, graph_id: str):
        self.deployment_ref = deployment_ref
        self.graph_id = graph_id
        super().__init__(
            f"deployment_ref {deployment_ref!r} is already bound to graph {graph_id!r}"
        )


def _row_get(row: object, column: str) -> str | None:
    """Read an optional column, tolerating rows that predate it.

    Greenfield runs migrations to head so the WS-D columns always exist, but
    guarding keeps hydration robust against a row mapping that lacks them.
    """
    try:
        value = row[column]  # type: ignore[index]
    except (KeyError, IndexError):
        return None
    return value if value else None


class SQLiteDeploymentRepository:
    """Persist and query deployment history using an async database."""

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    async def create(
        self,
        deployment: Deployment,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Deployment:
        """Insert a new deployment version and supersede older active versions."""
        owner_tenant = tenant_id if tenant_id is not None else deployment.tenant_id
        owner_workspace = workspace_id if tenant_id is not None else deployment.workspace_id
        if (deployment.tenant_id, deployment.workspace_id) != (
            owner_tenant,
            owner_workspace,
        ):
            raise ValueError("deployment owner does not match the requested scope")
        scope_sql, scope_params = _scope_clause(owner_tenant, owner_workspace)
        async with self._database.transaction() as connection:
            existing = await connection.fetch_one(
                """
                SELECT tenant_id, workspace_id, graph_id
                FROM deployment_versions
                WHERE deployment_ref = ?
                ORDER BY version DESC
                LIMIT 1
                """,
                (deployment.deployment_ref,),
            )
            if existing is not None and (existing["tenant_id"], existing["workspace_id"]) != (
                owner_tenant,
                owner_workspace,
            ):
                raise KeyError(deployment.deployment_ref)
            if existing is not None and existing["graph_id"] != deployment.graph_id:
                raise DeploymentRefLineageConflictError(
                    deployment.deployment_ref,
                    existing["graph_id"],
                )
            await connection.execute(
                f"""
                UPDATE deployment_versions
                SET status = ?, updated_at = ?
                WHERE deployment_ref = ? AND status = ? AND {scope_sql}
                """,
                (
                    DeploymentStatus.SUPERSEDED.value,
                    deployment.updated_at.isoformat(),
                    deployment.deployment_ref,
                    DeploymentStatus.ACTIVE.value,
                )
                + scope_params,
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
                    engine_mode,
                    attestation_payload_version,
                    entry_input_contract_ref,
                    entry_input_contract_version,
                    entry_output_contract_ref,
                    entry_output_contract_version,
                    deployment_settings_snapshot,
                    graph_snapshot_digest,
                    contract_snapshot_digest,
                    settings_snapshot_digest,
                    attestation_digest,
                    attestation_signature,
                    attestation_signing_key_id,
                    attestation_algorithm,
                    tenant_id,
                    workspace_id,
                    status,
                    created_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    deployment.deployment_id,
                    deployment.deployment_ref,
                    deployment.version,
                    deployment.graph_id,
                    deployment.graph_version,
                    deployment.graph_version_ref,
                    deployment.serialized_graph,
                    deployment.engine_mode.value,
                    deployment.attestation_payload_version,
                    deployment.entry_input_contract_ref,
                    deployment.entry_input_contract_version,
                    deployment.entry_output_contract_ref,
                    deployment.entry_output_contract_version,
                    to_json_value(deployment.deployment_settings_snapshot),
                    deployment.graph_snapshot_digest,
                    deployment.contract_snapshot_digest,
                    deployment.settings_snapshot_digest,
                    deployment.attestation_digest,
                    deployment.attestation_signature,
                    deployment.attestation_signing_key_id,
                    deployment.attestation_algorithm,
                    deployment.tenant_id,
                    deployment.workspace_id,
                    deployment.status.value,
                    deployment.created_at.isoformat(),
                    deployment.updated_at.isoformat(),
                ),
            )
        return await self.get(
            deployment.deployment_ref,
            deployment.version,
            tenant_id=owner_tenant,
            workspace_id=owner_workspace,
        )  # type: ignore[return-value]

    async def get(
        self,
        deployment_ref: str,
        version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Deployment | None:
        """Load the latest or a specific deployment version.

        WS-B: when ``tenant_id`` is supplied, a deployment owned by another
        tenant is invisible (returns ``None``). ``None`` = no tenant filter
        (internal deploy path, which is already deployment-ref scoped).
        """
        sql = """
            SELECT *
            FROM deployment_versions
            WHERE deployment_ref = ?
        """
        params: list[object] = [deployment_ref]
        if version is not None:
            sql += " AND version = ?"
            params.append(version)
        if tenant_id is not None:
            scope_sql, scope_params = _scope_clause(tenant_id, workspace_id)
            sql += f" AND {scope_sql}"
            params.extend(scope_params)
        sql += " ORDER BY version DESC LIMIT 1"
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(sql, tuple(params))
        if row is None:
            return None
        return self._row_to_deployment(row)

    async def list(
        self,
        deployment_ref: str | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Deployment]:
        """Return deployment history ordered from oldest to newest."""
        sql = "SELECT * FROM deployment_versions"
        clauses: list[str] = []
        params: list[object] = []
        if deployment_ref is not None:
            clauses.append("deployment_ref = ?")
            params.append(deployment_ref)
        if tenant_id is not None:
            scope_sql, scope_params = _scope_clause(tenant_id, workspace_id)
            clauses.append(scope_sql)
            params.extend(scope_params)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY deployment_ref, version"
        async with self._database.transaction() as connection:
            rows = await connection.fetch_all(sql, tuple(params))
        return [self._row_to_deployment(row) for row in rows]

    async def next_version(
        self,
        deployment_ref: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> int:
        """Return the next deployment version number for a stable deployment ref."""
        sql = """
            SELECT MAX(version) AS max_version
            FROM deployment_versions
            WHERE deployment_ref = ?
        """
        params: tuple[object, ...] = (deployment_ref,)
        if tenant_id is not None:
            scope_sql, scope_params = _scope_clause(tenant_id, workspace_id)
            sql += f" AND {scope_sql}"
            params += scope_params
        async with self._database.transaction() as connection:
            row = await connection.fetch_one(sql, params)
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
            engine_mode=DeploymentEngineMode(_row_get(row, "engine_mode") or "legacy"),
            attestation_payload_version=int(
                _row_get(row, "attestation_payload_version") or 1
            ),
            entry_input_contract_ref=row["entry_input_contract_ref"],
            entry_input_contract_version=row["entry_input_contract_version"],
            entry_output_contract_ref=row["entry_output_contract_ref"],
            entry_output_contract_version=row["entry_output_contract_version"],
            deployment_settings_snapshot=settings_snapshot,
            graph_snapshot_digest=graph_snapshot_digest,
            contract_snapshot_digest=contract_snapshot_digest,
            settings_snapshot_digest=settings_snapshot_digest,
            attestation_digest=row["attestation_digest"] or "",
            # Nullable signature columns (WS-D). Legacy rows carry NULL and
            # hydrate as unsigned-legacy — a signature cannot be recomputed
            # without the key, so there is no fallback here (unlike the digests).
            attestation_signature=_row_get(row, "attestation_signature"),
            attestation_signing_key_id=_row_get(row, "attestation_signing_key_id"),
            attestation_algorithm=_row_get(row, "attestation_algorithm"),
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


def _scope_clause(tenant_id: str, workspace_id: str | None) -> tuple[str, tuple[object, ...]]:
    """Build an exact tenant/workspace predicate; NULL is never a wildcard."""
    if workspace_id is None:
        return "tenant_id = ? AND workspace_id IS NULL", (tenant_id,)
    return "tenant_id = ? AND workspace_id = ?", (tenant_id, workspace_id)
