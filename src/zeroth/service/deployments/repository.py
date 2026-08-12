"""Async database-backed persistence for immutable deployment snapshots."""

from __future__ import annotations

from datetime import datetime

from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.json import load_typed_value, to_json_value
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)
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


@persistence_surface(
    "service.deployment_versions", probe=named_isolation_probe("_drive_deployments")
)
class SQLiteDeploymentRepository:
    """Persist and query deployment history using an async database."""

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    def _deployments(self, tenant_id: str | None, workspace_id: str | None) -> ScopedTable:
        tenant = tenant_id or "default"
        if workspace_id is None:
            context = (
                NullWorkspaceScopeContext.for_default_compatibility()
                if tenant == "default"
                else NullWorkspaceScopeContext(tenant_id=tenant)
            )
        else:
            context = (
                ScopeContext.for_default_compatibility(workspace_id=workspace_id)
                if tenant == "default"
                else ScopeContext(tenant_id=tenant, workspace_id=workspace_id)
            )
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.deployment_versions",
            context,
        )

    @persistence_operation(
        ResourceOperation.CREATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
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
        async with self._deployments(owner_tenant, owner_workspace).transaction(
            write_lock=True
        ) as deployments:
            existing = await deployments.select_one(
                where={"deployment_ref": deployment.deployment_ref},
                columns=("tenant_id", "workspace_id", "graph_id"),
                order_by_desc=("version",),
            )
            if existing is not None and existing["graph_id"] != deployment.graph_id:
                raise DeploymentRefLineageConflictError(
                    deployment.deployment_ref,
                    existing["graph_id"],
                )
            await deployments.update(
                {
                    "status": DeploymentStatus.SUPERSEDED.value,
                    "updated_at": deployment.updated_at.isoformat(),
                },
                where={
                    "deployment_ref": deployment.deployment_ref,
                    "status": DeploymentStatus.ACTIVE.value,
                },
            )
            inserted = await deployments.insert_if_absent(
                {
                    "deployment_id": deployment.deployment_id,
                    "deployment_ref": deployment.deployment_ref,
                    "version": deployment.version,
                    "graph_id": deployment.graph_id,
                    "graph_version": deployment.graph_version,
                    "graph_version_ref": deployment.graph_version_ref,
                    "serialized_graph": deployment.serialized_graph,
                    "engine_mode": deployment.engine_mode.value,
                    "attestation_payload_version": deployment.attestation_payload_version,
                    "entry_input_contract_ref": deployment.entry_input_contract_ref,
                    "entry_input_contract_version": deployment.entry_input_contract_version,
                    "entry_output_contract_ref": deployment.entry_output_contract_ref,
                    "entry_output_contract_version": deployment.entry_output_contract_version,
                    "deployment_settings_snapshot": to_json_value(
                        deployment.deployment_settings_snapshot
                    ),
                    "graph_snapshot_digest": deployment.graph_snapshot_digest,
                    "contract_snapshot_digest": deployment.contract_snapshot_digest,
                    "settings_snapshot_digest": deployment.settings_snapshot_digest,
                    "attestation_digest": deployment.attestation_digest,
                    "attestation_signature": deployment.attestation_signature,
                    "attestation_signing_key_id": deployment.attestation_signing_key_id,
                    "attestation_algorithm": deployment.attestation_algorithm,
                    "status": deployment.status.value,
                    "created_at": deployment.created_at.isoformat(),
                    "updated_at": deployment.updated_at.isoformat(),
                },
                conflict_columns=("deployment_ref", "version"),
            )
            if not inserted:
                raise KeyError(deployment.deployment_ref)
        return await self.get(
            deployment.deployment_ref,
            deployment.version,
            tenant_id=owner_tenant,
            workspace_id=owner_workspace,
        )  # type: ignore[return-value]

    @persistence_operation(ResourceOperation.READ)
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
        where: dict[str, object] = {"deployment_ref": deployment_ref}
        if version is not None:
            where["version"] = version
        async with self._deployments(tenant_id, workspace_id).transaction() as deployments:
            row = await deployments.select_one(where=where, order_by_desc=("version",))
        if row is None:
            return None
        return self._row_to_deployment(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list(
        self,
        deployment_ref: str | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Deployment]:
        """Return deployment history ordered from oldest to newest."""
        where: dict[str, object] = {}
        if deployment_ref is not None:
            where["deployment_ref"] = deployment_ref
        async with self._deployments(tenant_id, workspace_id).transaction() as deployments:
            rows = await deployments.select(
                where=where, order_by=("deployment_ref", "version")
            )
        return [self._row_to_deployment(row) for row in rows]

    @persistence_operation(ResourceOperation.READ)
    async def next_version(
        self,
        deployment_ref: str,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> int:
        """Return the next deployment version number for a stable deployment ref."""
        async with self._deployments(tenant_id, workspace_id).transaction() as deployments:
            row = await deployments.select_one(
                where={"deployment_ref": deployment_ref},
                columns=("version",),
                order_by_desc=("version",),
            )
        return (int(row["version"]) if row else 0) + 1

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
            attestation_payload_version=int(_row_get(row, "attestation_payload_version") or 1),
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
