"""Deployment-bound bootstrap wiring for the service wrapper."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import FastAPI

from zeroth.agent_runtime import AgentRunner
from zeroth.approvals import ApprovalRepository, ApprovalService
from zeroth.audit import AuditRepository
from zeroth.contracts import ContractRegistry
from zeroth.deployments import Deployment, DeploymentService, SQLiteDeploymentRepository
from zeroth.execution_units import ExecutableUnitRunner
from zeroth.graph import Graph, GraphRepository
from zeroth.graph.serialization import deserialize_graph
from zeroth.graph.versioning import graph_version_ref
from zeroth.orchestrator import RuntimeOrchestrator
from zeroth.runs import RunRepository, ThreadRepository
from zeroth.service.app import create_app
from zeroth.service.auth import JWTBearerTokenVerifier, ServiceAuthConfig, ServiceAuthenticator
from zeroth.storage import SQLiteDatabase


class DeploymentBootstrapError(RuntimeError):
    """Raised when the service cannot load the requested deployment."""


@dataclass(slots=True)
class ServiceBootstrap:
    """Container object for the deployment-scoped service surface."""

    deployment_service: DeploymentService
    deployment: Deployment
    graph: Graph
    run_repository: RunRepository
    thread_repository: ThreadRepository
    approval_service: ApprovalService
    audit_repository: AuditRepository
    contract_registry: ContractRegistry
    orchestrator: RuntimeOrchestrator
    auth_config: ServiceAuthConfig
    authenticator: ServiceAuthenticator


def bootstrap_service(
    database: SQLiteDatabase,
    *,
    deployment_ref: str,
    agent_runners: Mapping[str, AgentRunner] | None = None,
    executable_unit_runner: ExecutableUnitRunner | None = None,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
) -> ServiceBootstrap:
    """Build the service wrapper wiring for a specific deployment."""
    graph_repository = GraphRepository(database)
    deployment_repository = SQLiteDeploymentRepository(database)
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=deployment_repository,
        contract_registry=ContractRegistry(database),
    )
    deployment = deployment_service.get(deployment_ref)
    if deployment is None:
        raise DeploymentBootstrapError(f"deployment {deployment_ref!r} not found")

    try:
        graph = deserialize_graph(deployment.serialized_graph)
    except Exception as exc:  # pragma: no cover - defensive wrapper
        raise DeploymentBootstrapError(
            f"failed to deserialize deployment {deployment_ref!r}"
        ) from exc
    # Make sure the saved snapshot still matches the deployment metadata.
    if graph.graph_id != deployment.graph_id or graph.version != deployment.graph_version:
        raise DeploymentBootstrapError(
            "deployment graph snapshot does not match persisted graph metadata "
            f"for {deployment_ref!r}"
        )
    if deployment.graph_version_ref != graph_version_ref(graph.graph_id, graph.version):
        raise DeploymentBootstrapError(
            "deployment graph version ref does not match deserialized graph metadata "
            f"for {deployment_ref!r}"
        )

    run_repository = RunRepository(database)
    thread_repository = ThreadRepository(database)
    audit_repository = AuditRepository(database)
    approval_repository = ApprovalRepository(database)
    approval_service = ApprovalService(
        repository=approval_repository,
        run_repository=run_repository,
        audit_repository=audit_repository,
    )
    contract_registry = deployment_service.contract_registry
    resolved_agent_runners = dict(agent_runners or {})
    resolved_executable_unit_runner = executable_unit_runner or ExecutableUnitRunner()
    orchestrator = RuntimeOrchestrator(
        run_repository=run_repository,
        agent_runners=resolved_agent_runners,
        executable_unit_runner=resolved_executable_unit_runner,
        audit_repository=audit_repository,
        approval_service=approval_service,
    )
    resolved_auth_config = auth_config or ServiceAuthConfig.from_env()
    authenticator = ServiceAuthenticator(
        resolved_auth_config,
        bearer_verifier=bearer_token_verifier,
    )
    # Return one small container so the HTTP layer can stay thin and avoid global state.
    return ServiceBootstrap(
        deployment_service=deployment_service,
        deployment=deployment,
        graph=graph,
        run_repository=run_repository,
        thread_repository=thread_repository,
        approval_service=approval_service,
        audit_repository=audit_repository,
        contract_registry=contract_registry,
        orchestrator=orchestrator,
        auth_config=resolved_auth_config,
        authenticator=authenticator,
    )


def bootstrap_app(
    database: SQLiteDatabase,
    *,
    deployment_ref: str,
    agent_runners: Mapping[str, AgentRunner] | None = None,
    executable_unit_runner: ExecutableUnitRunner | None = None,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
) -> FastAPI:
    """Build the FastAPI app for a specific deployment."""
    return create_app(
        bootstrap_service(
            database,
            deployment_ref=deployment_ref,
            agent_runners=agent_runners,
            executable_unit_runner=executable_unit_runner,
            auth_config=auth_config,
            bearer_token_verifier=bearer_token_verifier,
        )
    )
