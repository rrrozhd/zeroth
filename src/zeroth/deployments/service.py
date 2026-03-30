"""Services for creating and querying immutable deployment snapshots."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from zeroth.contracts import ContractReference, ContractRegistry
from zeroth.contracts.errors import ContractNotFoundError
from zeroth.deployments.models import Deployment
from zeroth.deployments.provenance import (
    build_attestation_payload,
    compute_contract_snapshot_digest,
    compute_graph_snapshot_digest,
    compute_settings_snapshot_digest,
)
from zeroth.deployments.repository import SQLiteDeploymentRepository
from zeroth.graph import Graph, GraphRepository, GraphStatus
from zeroth.graph.serialization import serialize_graph
from zeroth.graph.versioning import graph_version_ref


class DeploymentError(RuntimeError):
    """Deployment-specific business rule failure."""


@dataclass(slots=True)
class DeploymentService:
    """Deploy published graph snapshots and query deployment history."""

    graph_repository: GraphRepository
    deployment_repository: SQLiteDeploymentRepository
    contract_registry: ContractRegistry | None = None

    def deploy(
        self,
        deployment_ref: str,
        graph_id: str,
        graph_version: int | None = None,
    ) -> Deployment:
        """Create a new immutable deployment version from a published graph."""
        self._ensure_deployment_ref_lineage(deployment_ref, graph_id)
        graph = self._require_published_graph(graph_id, graph_version)
        entry_node = self._entry_node(graph)
        # Contract versions are pinned now so the deployment keeps using the same schema later.
        input_contract_version = self._resolve_contract_version(
            entry_node.input_contract_ref if entry_node else None
        )
        output_contract_version = self._resolve_contract_version(
            entry_node.output_contract_ref if entry_node else None
        )
        last_error: sqlite3.IntegrityError | None = None
        for _ in range(3):
            # Version allocation can race, so retry a few times on conflicts.
            tenant_id = str(graph.deployment_settings.get("tenant_id", "default"))
            workspace_id = graph.deployment_settings.get("workspace_id")
            serialized_graph = serialize_graph(graph)
            deployment = Deployment(
                deployment_id=uuid4().hex,
                deployment_ref=deployment_ref,
                version=self.deployment_repository.next_version(deployment_ref),
                graph_id=graph.graph_id,
                graph_version=graph.version,
                graph_version_ref=graph_version_ref(graph.graph_id, graph.version),
                serialized_graph=serialized_graph,
                entry_input_contract_ref=entry_node.input_contract_ref if entry_node else None,
                entry_input_contract_version=input_contract_version,
                entry_output_contract_ref=entry_node.output_contract_ref if entry_node else None,
                entry_output_contract_version=output_contract_version,
                deployment_settings_snapshot=dict(graph.deployment_settings),
                graph_snapshot_digest=compute_graph_snapshot_digest(serialized_graph),
                contract_snapshot_digest=compute_contract_snapshot_digest(
                    entry_input_contract_ref=entry_node.input_contract_ref if entry_node else None,
                    entry_input_contract_version=input_contract_version,
                    entry_output_contract_ref=(
                        entry_node.output_contract_ref if entry_node else None
                    ),
                    entry_output_contract_version=output_contract_version,
                ),
                settings_snapshot_digest=compute_settings_snapshot_digest(dict(graph.deployment_settings)),
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            deployment.attestation_digest = str(
                build_attestation_payload(deployment)["attestation_digest"]
            )
            try:
                return self.deployment_repository.create(deployment)
            except sqlite3.IntegrityError as exc:
                last_error = exc
        raise DeploymentError(
            f"failed to allocate deployment version for {deployment_ref!r} after retries"
        ) from last_error

    def get(self, deployment_ref: str, version: int | None = None) -> Deployment | None:
        """Load the latest or a specific deployment version."""
        return self.deployment_repository.get(deployment_ref, version)

    def list(self, deployment_ref: str | None = None) -> list[Deployment]:
        """Return deployment history, optionally scoped to one ref."""
        return self.deployment_repository.list(deployment_ref)

    def rollback(self, deployment_ref: str, *, target_graph_version: int) -> Deployment:
        """Redeploy an earlier published graph version under the same deployment ref."""
        current = self.deployment_repository.get(deployment_ref)
        if current is None:
            raise KeyError(deployment_ref)
        return self.deploy(deployment_ref, current.graph_id, target_graph_version)

    def _require_published_graph(self, graph_id: str, version: int | None) -> Graph:
        """Load a graph version and ensure it is published before deployment."""
        graph = (
            self.graph_repository.get(graph_id, version)
            if version is not None
            else self._latest_published_graph(graph_id)
        )
        if graph is None:
            if version is None:
                raise DeploymentError(f"graph {graph_id} has no published versions to deploy")
            raise KeyError(f"{graph_id}@{version}")
        if graph.status is not GraphStatus.PUBLISHED:
            msg = f"graph version {graph.graph_id}@{graph.version} must be published before deploy"
            raise DeploymentError(msg)
        return graph

    def _latest_published_graph(self, graph_id: str) -> Graph | None:
        """Return the newest published graph version for a graph lineage."""
        for graph in reversed(self.graph_repository.list_versions(graph_id)):
            if graph.status is GraphStatus.PUBLISHED:
                # Latest published wins when the caller does not pin a graph version.
                return graph
        return None

    def _ensure_deployment_ref_lineage(self, deployment_ref: str, graph_id: str) -> None:
        """Reject rebinding an existing deployment ref to a different graph lineage."""
        existing = self.deployment_repository.get(deployment_ref)
        if existing is None or existing.graph_id == graph_id:
            return
        msg = (
            f"deployment_ref {deployment_ref!r} is already bound to graph "
            f"{existing.graph_id!r} and cannot be reused for {graph_id!r}"
        )
        raise DeploymentError(msg)

    def _entry_node(self, graph: Graph):
        """Resolve the entry node so its contracts are snapshotted at deploy time."""
        if not graph.nodes:
            return None
        entry_step = graph.entry_step or graph.nodes[0].node_id
        for node in graph.nodes:
            if node.node_id == entry_step:
                return node
        msg = f"graph {graph.graph_id}@{graph.version} has unknown entry step {entry_step}"
        raise DeploymentError(msg)

    def _resolve_contract_version(self, contract_ref: str | None) -> int | None:
        """Pin the active contract version into the deployment snapshot."""
        if contract_ref is None:
            return None
        if self.contract_registry is None:
            raise DeploymentError(f"contract registry is required to deploy {contract_ref!r}")
        try:
            contract = self.contract_registry.resolve(ContractReference(name=contract_ref))
        except ContractNotFoundError as exc:
            raise DeploymentError(
                f"deployment contract {contract_ref!r} is not registered"
            ) from exc
        return contract.version
