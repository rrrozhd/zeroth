"""Deployment-bound bootstrap wiring for the service wrapper."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import FastAPI

from zeroth.core.agent_runtime import AgentRunner
from zeroth.core.approvals import ApprovalRepository, ApprovalService
from zeroth.core.audit import AuditRepository
from zeroth.core.config.settings import get_settings
from zeroth.core.contracts import ContractRegistry
from zeroth.core.deployments import Deployment, DeploymentService, SQLiteDeploymentRepository
from zeroth.core.dispatch import LeaseManager, RunWorker
from zeroth.core.econ.client import RegulusClient
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import Graph, GraphRepository
from zeroth.core.graph.serialization import deserialize_graph
from zeroth.core.graph.versioning import graph_version_ref
from zeroth.core.guardrails.config import GuardrailConfig
from zeroth.core.guardrails.dead_letter import DeadLetterManager
from zeroth.core.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
from zeroth.core.memory.factory import register_memory_connectors
from zeroth.core.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.core.observability.metrics import MetricsCollector
from zeroth.core.observability.queue_gauge import QueueDepthGauge
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.runs import RunRepository, ThreadRepository
from zeroth.core.service.app import create_app
from zeroth.core.service.auth import JWTBearerTokenVerifier, ServiceAuthConfig, ServiceAuthenticator
from zeroth.core.storage import AsyncDatabase


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
    migrations_dir = str(importlib.resources.files("zeroth.core.migrations"))
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
    # Phase 20: Memory resolver for dispatch-time injection.
    memory_resolver: object | None = None
    # Phase 17: Database reference for health probes.
    database: AsyncDatabase | None = None
    # Phase 15: Webhook and SLA components (optional).
    webhook_service: object | None = None
    webhook_repository: object | None = None
    delivery_worker: object | None = None
    sla_checker: object | None = None
    webhook_http_client: object | None = None
    # Phase 18: Cross-phase wiring.
    cost_estimator: object | None = None
    arq_pool: object | None = None
    redis_client: object | None = None
    # Phase 34: Artifact store for large payload externalization.
    artifact_store: object | None = None
    # Phase 35: Resilient HTTP client.
    http_client: object | None = None
    # Phase 36: Template registry for prompt template management.
    template_registry: object | None = None
    # Phase 37: Context window management is enabled by default.
    # Per-node settings on AgentNodeData control whether compaction is active.
    # No explicit bootstrap wiring needed -- orchestrator.context_window_enabled defaults True.


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

    # Phase 13: Regulus economics integration.
    settings = get_settings()
    regulus_client: RegulusClient | None = None
    budget_enforcer: object | None = None
    cost_estimator: object | None = None
    if settings.regulus.enabled:
        regulus_client = RegulusClient(
            base_url=settings.regulus.base_url,
            timeout=settings.regulus.request_timeout,
            enabled=True,
        )
        # BudgetEnforcer wired here once econ.budget module lands (Plan 13-02).
        try:
            from zeroth.core.econ.budget import BudgetEnforcer

            budget_enforcer = BudgetEnforcer(
                regulus_base_url=settings.regulus.base_url,
                cache_ttl=settings.regulus.budget_cache_ttl,
                timeout=settings.regulus.request_timeout,
            )
        except ImportError:
            pass
        try:
            from zeroth.core.econ.cost import CostEstimator

            cost_estimator = CostEstimator()
        except ImportError:
            cost_estimator = None

    # Phase 18: Wire cost instrumentation into orchestrator.
    orchestrator.regulus_client = regulus_client
    orchestrator.cost_estimator = cost_estimator
    orchestrator.deployment_ref = deployment.deployment_ref

    # Phase 16/18: ARQ wakeup pool.
    arq_pool = None
    if settings.dispatch.arq_enabled:
        try:
            from zeroth.core.dispatch.arq_wakeup import create_arq_pool

            arq_pool = await create_arq_pool(settings.redis)
        except ImportError:
            pass

    # Phase 14/18: Memory connector registration with real settings.
    memory_registry = InMemoryConnectorRegistry()
    redis_client = None
    if settings.redis.mode != "disabled":
        try:
            import redis.asyncio as aioredis

            redis_url = f"redis://{settings.redis.host}:{settings.redis.port}/{settings.redis.db}"
            if settings.redis.password:
                redis_url = f"redis://:{settings.redis.password.get_secret_value()}@{settings.redis.host}:{settings.redis.port}/{settings.redis.db}"
            redis_client = aioredis.from_url(redis_url)
        except ImportError:
            pass

    pg_conninfo = None
    if settings.database.backend == "postgres" and settings.database.postgres_dsn:
        pg_conninfo = settings.database.postgres_dsn.get_secret_value()

    register_memory_connectors(
        memory_registry, settings, redis_client=redis_client, pg_conninfo=pg_conninfo
    )

    # Phase 20: Create resolver from populated registry for AgentRunner injection.
    memory_resolver = MemoryConnectorResolver(
        registry=memory_registry,
        thread_repository=thread_repository,
    )

    # Phase 20: Wire memory resolver and budget enforcer into orchestrator.
    orchestrator.memory_resolver = memory_resolver
    orchestrator.budget_enforcer = budget_enforcer

    # Phase 34: Artifact store construction and wiring.
    artifact_store: object | None = None
    artifact_settings = settings.artifact_store
    if artifact_settings.backend == "filesystem":
        from zeroth.core.artifacts.store import FilesystemArtifactStore

        artifact_store = FilesystemArtifactStore(
            base_dir=artifact_settings.filesystem_base_dir,
            default_ttl=artifact_settings.default_ttl_seconds,
            max_size=artifact_settings.max_artifact_size_bytes,
        )
    elif artifact_settings.backend == "redis" and redis_client is not None:
        from zeroth.core.artifacts.store import RedisArtifactStore

        artifact_store = RedisArtifactStore(
            redis_url="",  # not used when client is provided
            prefix=artifact_settings.redis_key_prefix,
            default_ttl=artifact_settings.default_ttl_seconds,
            max_size=artifact_settings.max_artifact_size_bytes,
            client=redis_client,
        )
    elif artifact_settings.backend not in ("filesystem", "redis"):
        raise ValueError(
            f"Unknown artifact store backend: {artifact_settings.backend!r}. "
            "Must be 'filesystem' or 'redis'."
        )
    orchestrator.artifact_store = artifact_store

    # Phase 35: Resilient HTTP client construction.
    http_client_instance: object | None = None
    http_settings = settings.http_client
    import os  # noqa: PLC0415

    from zeroth.core.http import ResilientHttpClient  # noqa: PLC0415
    from zeroth.core.secrets import EnvSecretProvider  # noqa: PLC0415

    env_secret_provider = EnvSecretProvider(os.environ)
    http_client_instance = ResilientHttpClient(
        settings=http_settings,
        secret_provider=env_secret_provider,
    )
    orchestrator.http_client = http_client_instance

    # Phase 36: Template registry and renderer.
    from zeroth.core.templates import TemplateRegistry, TemplateRenderer  # noqa: PLC0415

    template_registry = TemplateRegistry()
    template_renderer = TemplateRenderer()
    orchestrator.template_registry = template_registry
    orchestrator.template_renderer = template_renderer

    # Phase 15: Webhook delivery and SLA enforcement.
    webhook_repository = None
    webhook_service_obj = None
    delivery_worker_obj = None
    sla_checker_obj = None
    webhook_http_client = None

    if settings.webhook.enabled:
        try:
            from zeroth.core.webhooks.repository import WebhookRepository
            from zeroth.core.webhooks.service import WebhookService

            webhook_repository = WebhookRepository(database)
            webhook_service_obj = WebhookService(
                repository=webhook_repository,
                default_max_retries=settings.webhook.default_max_retries,
            )
            # Wire webhook_service into orchestrator and approval_service
            orchestrator.webhook_service = webhook_service_obj
            approval_service.webhook_service = webhook_service_obj

            import httpx

            from zeroth.core.webhooks.delivery import WebhookDeliveryWorker

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
            from zeroth.core.approvals.sla_checker import ApprovalSLAChecker

            sla_checker_obj = ApprovalSLAChecker(
                approval_service=approval_service,
                webhook_service=webhook_service_obj,
                poll_interval=settings.approval_sla.checker_poll_interval,
            )
        except ImportError:
            pass

    return ServiceBootstrap(
        database=database,
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
        memory_resolver=memory_resolver,
        webhook_service=webhook_service_obj,
        webhook_repository=webhook_repository,
        delivery_worker=delivery_worker_obj,
        sla_checker=sla_checker_obj,
        webhook_http_client=webhook_http_client,
        cost_estimator=cost_estimator,
        arq_pool=arq_pool,
        redis_client=redis_client,
        artifact_store=artifact_store,
        http_client=http_client_instance,
        template_registry=template_registry,
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
