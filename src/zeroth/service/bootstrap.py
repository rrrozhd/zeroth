"""Deployment-bound bootstrap wiring for the service wrapper."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import FastAPI

from zeroth.agent_runtime import AgentRunner
from zeroth.approvals import ApprovalRepository, ApprovalService
from zeroth.audit import AuditRepository
from zeroth.config.settings import get_settings
from zeroth.contracts import ContractRegistry
from zeroth.deployments import Deployment, DeploymentService, SQLiteDeploymentRepository
from zeroth.dispatch import LeaseManager, RunWorker
from zeroth.econ.client import RegulusClient
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
from zeroth.storage import AsyncDatabase


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


def run_migrations(database_url: str) -> None:
    """Run Alembic migrations against the given database URL."""
    import importlib.resources

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config()
    migrations_dir = str(importlib.resources.files("zeroth.migrations"))
    alembic_cfg.set_main_option("script_location", migrations_dir)
    alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_cfg, "head")


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
    # Phase 13: Regulus economics integration (optional).
    regulus_client: RegulusClient | None = None
    budget_enforcer: object | None = None
    # Phase 14: Memory connector registry (populated at bootstrap).
    memory_registry: InMemoryConnectorRegistry | None = None
    # Phase 16: ARQ wakeup pool (optional).
    arq_pool: object | None = None
    # Phase 15: Webhook and SLA components (optional).
    webhook_service: object | None = None
    webhook_repository: object | None = None
    delivery_worker: object | None = None
    sla_checker: object | None = None
    webhook_http_client: object | None = None


async def bootstrap_service(
    database: AsyncDatabase,
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
    deployment = await deployment_service.get(deployment_ref)
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
            "deployment graph snapshot does not match persisted graph "
            f"metadata for {deployment_ref!r}"
        )
    if deployment.graph_version_ref != graph_version_ref(graph.graph_id, graph.version):
        raise DeploymentBootstrapError(
            "deployment graph version ref does not match deserialized "
            f"graph metadata for {deployment_ref!r}"
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

    settings = get_settings()

    worker: RunWorker | None = None
    if enable_durable_worker:
        worker = RunWorker(
            deployment_ref=deployment.deployment_ref,
            run_repository=run_repository,
            orchestrator=orchestrator,
            graph=graph,
            lease_manager=lease_manager,
            max_concurrency=resolved_guardrail_config.max_concurrency,
            poll_interval=settings.dispatch.poll_interval,
            shutdown_timeout=settings.dispatch.shutdown_timeout,
            dead_letter_manager=dead_letter_manager,
            metrics_collector=metrics_collector,
        )

    # Phase 13: Regulus economics integration.
    regulus_client: RegulusClient | None = None
    budget_enforcer: object | None = None
    if settings.regulus.enabled:
        regulus_client = RegulusClient(
            base_url=settings.regulus.base_url,
            timeout=settings.regulus.request_timeout,
            enabled=True,
        )
        # BudgetEnforcer wired here once econ.budget module lands (Plan 13-02).
        try:
            from zeroth.econ.budget import BudgetEnforcer

            budget_enforcer = BudgetEnforcer(
                regulus_base_url=settings.regulus.base_url,
                cache_ttl=settings.regulus.budget_cache_ttl,
                timeout=settings.regulus.request_timeout,
            )
        except ImportError:
            pass

    # Phase 14: Memory connector registration.
    memory_registry = InMemoryConnectorRegistry()
    register_memory_connectors(memory_registry, _BootstrapMemorySettings())

    # Phase 15: Webhook delivery and SLA enforcement.
    webhook_repository = None
    webhook_service_obj = None
    delivery_worker_obj = None
    sla_checker_obj = None
    webhook_http_client = None

    if settings.webhook.enabled:
        try:
            from zeroth.webhooks.repository import WebhookRepository
            from zeroth.webhooks.service import WebhookService

            webhook_repository = WebhookRepository(database)
            webhook_service_obj = WebhookService(
                repository=webhook_repository,
                default_max_retries=settings.webhook.default_max_retries,
            )
            # Wire webhook_service into orchestrator and approval_service
            orchestrator.webhook_service = webhook_service_obj
            approval_service.webhook_service = webhook_service_obj

            import httpx

            from zeroth.webhooks.delivery import WebhookDeliveryWorker

            webhook_http_client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                timeout=httpx.Timeout(settings.webhook.delivery_timeout),
            )
            delivery_worker_obj = WebhookDeliveryWorker(
                repository=webhook_repository,
                http_client=webhook_http_client,
                poll_interval=settings.webhook.delivery_poll_interval,
                max_concurrency=settings.webhook.max_delivery_concurrency,
                retry_base_delay=settings.webhook.retry_base_delay,
                retry_max_delay=settings.webhook.retry_max_delay,
            )
        except ImportError:
            pass

    if settings.approval_sla.enabled:
        try:
            from zeroth.approvals.sla_checker import ApprovalSLAChecker

            sla_checker_obj = ApprovalSLAChecker(
                approval_service=approval_service,
                webhook_service=webhook_service_obj,
                poll_interval=settings.approval_sla.checker_poll_interval,
            )
        except ImportError:
            pass

    # Phase 16: ARQ wakeup pool for low-latency dispatch.
    arq_pool = None
    if settings.dispatch.arq_enabled:
        try:
            from zeroth.dispatch.arq_wakeup import create_arq_pool

            arq_pool = await create_arq_pool(settings.redis)
        except ImportError:
            pass

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
        regulus_client=regulus_client,
        budget_enforcer=budget_enforcer,
        memory_registry=memory_registry,
        arq_pool=arq_pool,
        webhook_service=webhook_service_obj,
        webhook_repository=webhook_repository,
        delivery_worker=delivery_worker_obj,
        sla_checker=sla_checker_obj,
        webhook_http_client=webhook_http_client,
    )


async def bootstrap_app(
    database: AsyncDatabase,
    *,
    deployment_ref: str,
    agent_runners: Mapping[str, AgentRunner] | None = None,
    executable_unit_runner: ExecutableUnitRunner | None = None,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
) -> FastAPI:
    """Build the FastAPI app for a specific deployment."""
    return create_app(
        await bootstrap_service(
            database,
            deployment_ref=deployment_ref,
            agent_runners=agent_runners,
            executable_unit_runner=executable_unit_runner,
            auth_config=auth_config,
            bearer_token_verifier=bearer_token_verifier,
        )
    )
