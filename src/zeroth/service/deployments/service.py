"""Services for creating and querying immutable deployment snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from zeroth.contracts.graph import Graph, GraphRepository, GraphStatus, Node
from zeroth.contracts.graph.engine_mode import effective_engine_mode, explicit_legacy_engine
from zeroth.contracts.graph.serialization import serialize_graph
from zeroth.contracts.graph.versioning import graph_version_ref
from zeroth.contracts.graph.warnings import warn_legacy_engine
from zeroth.contracts.registry import (
    ContractReference,
    ContractRegistry,
    contract_scope_context,
)
from zeroth.contracts.registry.errors import ContractNotFoundError
from zeroth.service.deployments.models import Deployment, DeploymentEngineMode
from zeroth.service.deployments.provenance import (
    build_attestation_payload,
    compute_contract_snapshot_digest,
    compute_graph_snapshot_digest,
    compute_settings_snapshot_digest,
    sign_attestation,
)
from zeroth.service.deployments.repository import (
    DeploymentRefLineageConflictError,
    SQLiteDeploymentRepository,
)

if TYPE_CHECKING:
    from zeroth.platform.signing import SigningKeyProvider


class DeploymentError(RuntimeError):
    """Deployment-specific business rule failure."""


@dataclass(slots=True)
class DeploymentService:
    """Deploy published graph snapshots and query deployment history."""

    graph_repository: GraphRepository
    deployment_repository: SQLiteDeploymentRepository
    contract_registry: ContractRegistry | None = None
    # WS-D signer: when None the attestation is stored unsigned-legacy. Injected
    # post-construction by bootstrap once the shared secret provider is built.
    signer: SigningKeyProvider | None = None

    async def deploy(
        self,
        deployment_ref: str,
        graph_id: str,
        graph_version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Deployment:
        """Create a new immutable deployment version from a published graph."""
        await self._ensure_deployment_ref_lineage(
            deployment_ref,
            graph_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        graph = await self._require_published_graph(
            graph_id,
            graph_version,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        entry_node = self._entry_node(graph)
        # Contract versions are pinned now so the deployment keeps using
        # the same schema later.
        input_contract_version = await self._resolve_contract_version(
            entry_node.input_contract_ref if entry_node else None,
            tenant_id=graph.tenant_id,
            workspace_id=graph.workspace_id,
        )
        output_contract_version = await self._resolve_contract_version(
            entry_node.output_contract_ref if entry_node else None,
            tenant_id=graph.tenant_id,
            workspace_id=graph.workspace_id,
        )
        last_error: Exception | None = None
        for _ in range(3):
            # Version allocation can race, so retry a few times on conflicts.
            serialized_graph = serialize_graph(graph)
            deployment = Deployment(
                deployment_id=uuid4().hex,
                deployment_ref=deployment_ref,
                version=await self.deployment_repository.next_version(
                    deployment_ref,
                    tenant_id=graph.tenant_id,
                    workspace_id=graph.workspace_id,
                ),
                graph_id=graph.graph_id,
                graph_version=graph.version,
                graph_version_ref=graph_version_ref(graph.graph_id, graph.version),
                serialized_graph=serialized_graph,
                engine_mode=DeploymentEngineMode(effective_engine_mode(graph.execution_settings)),
                attestation_payload_version=2,
                entry_input_contract_ref=(entry_node.input_contract_ref if entry_node else None),
                entry_input_contract_version=input_contract_version,
                entry_output_contract_ref=(entry_node.output_contract_ref if entry_node else None),
                entry_output_contract_version=output_contract_version,
                deployment_settings_snapshot=dict(graph.deployment_settings),
                graph_snapshot_digest=compute_graph_snapshot_digest(serialized_graph),
                contract_snapshot_digest=compute_contract_snapshot_digest(
                    entry_input_contract_ref=(
                        entry_node.input_contract_ref if entry_node else None
                    ),
                    entry_input_contract_version=input_contract_version,
                    entry_output_contract_ref=(
                        entry_node.output_contract_ref if entry_node else None
                    ),
                    entry_output_contract_version=output_contract_version,
                ),
                settings_snapshot_digest=compute_settings_snapshot_digest(
                    dict(graph.deployment_settings)
                ),
                tenant_id=graph.tenant_id,
                workspace_id=graph.workspace_id,
            )
            if explicit_legacy_engine(graph.execution_settings):
                warn_legacy_engine(stage="deployment_publication", stacklevel=2)
            deployment.attestation_digest = str(
                build_attestation_payload(deployment)["attestation_digest"]
            )
            # Sign inside the version-allocation retry loop so the row that is
            # ultimately persisted always carries a signature over ITS digest.
            signature, key_id, algorithm = sign_attestation(
                deployment.attestation_digest, self.signer
            )
            deployment.attestation_signature = signature
            deployment.attestation_signing_key_id = key_id
            deployment.attestation_algorithm = algorithm
            try:
                return await self.deployment_repository.create(
                    deployment,
                    tenant_id=graph.tenant_id,
                    workspace_id=graph.workspace_id,
                )
            except DeploymentRefLineageConflictError as exc:
                msg = (
                    f"deployment_ref {deployment_ref!r} is already bound to graph "
                    f"{exc.graph_id!r} and cannot be reused for {graph_id!r}"
                )
                raise DeploymentError(msg) from exc
            except KeyError:
                raise
            except Exception as exc:
                last_error = exc
        raise DeploymentError(
            f"failed to allocate deployment version for {deployment_ref!r} after retries"
        ) from last_error

    async def get(
        self,
        deployment_ref: str,
        version: int | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Deployment | None:
        """Load the latest or a specific deployment version (optionally tenant-scoped)."""
        return await self.deployment_repository.get(
            deployment_ref,
            version,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

    async def list(
        self,
        deployment_ref: str | None = None,
        *,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Deployment]:
        """Return deployment history, optionally scoped to one ref and/or tenant."""
        return await self.deployment_repository.list(
            deployment_ref,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

    async def rollback(
        self,
        deployment_ref: str,
        *,
        target_graph_version: int,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Deployment:
        """Redeploy an earlier published graph version under the same ref."""
        current = await self.deployment_repository.get(
            deployment_ref,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        if current is None:
            raise KeyError(deployment_ref)
        return await self.deploy(
            deployment_ref,
            current.graph_id,
            target_graph_version,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

    async def _require_published_graph(
        self,
        graph_id: str,
        version: int | None,
        *,
        tenant_id: str | None,
        workspace_id: str | None,
    ) -> Graph:
        """Load a graph version and ensure it is published before deploy."""
        graph = (
            await self.graph_repository.get(
                graph_id,
                version,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            if version is not None
            else await self._latest_published_graph(
                graph_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
        )
        if graph is None:
            if version is None:
                visible_graph = await self.graph_repository.get(
                    graph_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                )
                if tenant_id is not None and visible_graph is None:
                    raise KeyError(graph_id)
                raise DeploymentError(f"graph {graph_id} has no published versions to deploy")
            raise KeyError(f"{graph_id}@{version}")
        if graph.status is not GraphStatus.PUBLISHED:
            msg = f"graph version {graph.graph_id}@{graph.version} must be published before deploy"
            raise DeploymentError(msg)
        return graph

    async def _latest_published_graph(
        self,
        graph_id: str,
        *,
        tenant_id: str | None,
        workspace_id: str | None,
    ) -> Graph | None:
        """Return the newest published graph version for a graph lineage."""
        graphs = await self.graph_repository.list_versions(
            graph_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        for graph in reversed(graphs):
            if graph.status is GraphStatus.PUBLISHED:
                return graph
        return None

    async def _ensure_deployment_ref_lineage(
        self,
        deployment_ref: str,
        graph_id: str,
        *,
        tenant_id: str | None,
        workspace_id: str | None,
    ) -> None:
        """Reject rebinding a deployment ref to a different graph lineage."""
        existing = await self.deployment_repository.get(
            deployment_ref,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        if existing is None and tenant_id is not None:
            if await self.deployment_repository.get(deployment_ref) is not None:
                raise KeyError(deployment_ref)
            return
        if existing is None or existing.graph_id == graph_id:
            return
        msg = (
            f"deployment_ref {deployment_ref!r} is already bound to graph "
            f"{existing.graph_id!r} and cannot be reused for {graph_id!r}"
        )
        raise DeploymentError(msg)

    def _entry_node(self, graph: Graph) -> Node | None:
        """Resolve the entry node for contract snapshotting."""
        if not graph.nodes:
            return None
        entry_step = graph.entry_step or graph.nodes[0].node_id
        for node in graph.nodes:
            if node.node_id == entry_step:
                return node
        msg = f"graph {graph.graph_id}@{graph.version} has unknown entry step {entry_step}"
        raise DeploymentError(msg)

    async def _resolve_contract_version(
        self,
        contract_ref: str | None,
        *,
        tenant_id: str,
        workspace_id: str | None,
    ) -> int | None:
        """Pin the active contract version into the deployment snapshot."""
        if contract_ref is None:
            return None
        if self.contract_registry is None:
            raise DeploymentError(f"contract registry is required to deploy {contract_ref!r}")
        registry = self.contract_registry.for_scope(contract_scope_context(tenant_id, workspace_id))
        try:
            contract = await registry.resolve(ContractReference(name=contract_ref))
        except ContractNotFoundError as exc:
            raise DeploymentError(
                f"deployment contract {contract_ref!r} is not registered"
            ) from exc
        return contract.version
