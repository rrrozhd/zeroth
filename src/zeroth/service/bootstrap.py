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
from zeroth.dispatch import LeaseManager, RunWorker
from zeroth.execution_units import ExecutableUnitRunner
from zeroth.graph import Graph, GraphRepository
from zeroth.graph.serialization import deserialize_graph
from zeroth.graph.versioning import graph_version_ref
from zeroth.guardrails.config import GuardrailConfig
from zeroth.guardrails.dead_letter import DeadLetterManager
from zeroth.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
from zeroth.memory.factory import register_memory_connectors
from zeroth.memory.registry import InMemoryConnectorRegistry
from zeroth.observability.metrics import MetricsCollector
from zeroth.observability.queue_gauge import QueueDepthGauge
from zeroth.orchestrator import RuntimeOrchestrator
from zeroth.runs import RunRepository, ThreadRepository
from zeroth.service.app import create_app
from zeroth.service.auth import JWTBearerTokenVerifier, ServiceAuthConfig, ServiceAuthenticator
from zeroth.storage import SQLiteDatabase


class _BootstrapMemorySubsection:
    """Tiny helper providing default attribute values for memory sub-settings."""

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _BootstrapMemorySettings:
    """Default memory settings used by bootstrap when no ZerothSettings is available.

    Provides the attribute shape expected by ``register_memory_connectors``:
    ``memory``, ``pgvector``, ``chroma``, and ``elasticsearch`` sub-objects.
    All external backends are disabled by default; only in-memory connectors
    are registered.
    """

    def __init__(self) -> None:
        self.memory = _BootstrapMemorySubsection(
            default_connector="ephemeral",
            redis_kv_prefix="zeroth:mem:kv",
            redis_thread_prefix="zeroth:mem:thread",
        )
        self.pgvector = _BootstrapMemorySubsection(
            enabled=False,
            table_name="zeroth_memory_vectors",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        )
        self.chroma = _BootstrapMemorySubsection(
            enabled=False,
            host="localhost",
            port=8000,
            collection_prefix="zeroth_memory",
        )
        self.elasticsearch = _BootstrapMemorySubsection(
            enabled=False,
            hosts=["http://localhost:9200"],
            index_prefix="zeroth_memory",
        )


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
    # Phase 9 additions (optional so existing tests don't break).
    worker: RunWorker | None = None
    lease_manager: LeaseManager | None = None
    guardrail_config: GuardrailConfig | None = None
    rate_limiter: TokenBucketRateLimiter | None = None
    quota_enforcer: QuotaEnforcer | None = None
    dead_letter_manager: DeadLetterManager | None = None
    metrics_collector: MetricsCollector | None = None
    queue_gauge: QueueDepthGauge | None = None
    # Phase 14: Memory connector registry (populated at bootstrap).
    memory_registry: InMemoryConnectorRegistry | None = None


def bootstrap_service(
    database: SQLiteDatabase,
    *,
    deployment_ref: str,
    agent_runners: Mapping[str, AgentRunner] | None = None,
    executable_unit_runner: ExecutableUnitRunner | None = None,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
    guardrail_config: GuardrailConfig | None = None,
    enable_durable_worker: bool = True,
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

    # Phase 9: durable dispatch, guardrails, observability.
    resolved_guardrail_config = guardrail_config or GuardrailConfig()
    lease_manager = LeaseManager(database)
    dead_letter_manager = DeadLetterManager(
        run_repository=run_repository,
        max_failure_count=resolved_guardrail_config.max_failure_count,
    )
    rate_limiter = TokenBucketRateLimiter(database)
    quota_enforcer = QuotaEnforcer(database)
    metrics_collector = MetricsCollector()
    queue_gauge = QueueDepthGauge(
        run_repository=run_repository,
        deployment_ref=deployment.deployment_ref,
        metrics_collector=metrics_collector,
    )

    worker: RunWorker | None = None
    if enable_durable_worker:
        worker = RunWorker(
            deployment_ref=deployment.deployment_ref,
            run_repository=run_repository,
            orchestrator=orchestrator,
            graph=graph,
            lease_manager=lease_manager,
            max_concurrency=resolved_guardrail_config.max_concurrency,
            dead_letter_manager=dead_letter_manager,
            metrics_collector=metrics_collector,
        )

    # Phase 14: Memory connector registration.
    memory_registry = InMemoryConnectorRegistry()
    register_memory_connectors(memory_registry, _BootstrapMemorySettings())

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
        worker=worker,
        lease_manager=lease_manager,
        guardrail_config=resolved_guardrail_config,
        rate_limiter=rate_limiter,
        quota_enforcer=quota_enforcer,
        dead_letter_manager=dead_letter_manager,
        metrics_collector=metrics_collector,
        queue_gauge=queue_gauge,
        memory_registry=memory_registry,
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
