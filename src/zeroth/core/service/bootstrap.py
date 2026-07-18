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
from zeroth.core.graph.validation import GraphValidator
from zeroth.core.graph.versioning import graph_version_ref
from zeroth.core.guardrails.config import GuardrailConfig
from zeroth.core.guardrails.dead_letter import DeadLetterManager
from zeroth.core.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
from zeroth.core.memory.config_repository import MemoryConnectorConfigRepository
from zeroth.core.memory.factory import register_memory_connectors
from zeroth.core.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.core.memory.runtime_configs import load_persisted_connectors
from zeroth.core.observability.metrics import MetricsCollector
from zeroth.core.observability.queue_gauge import QueueDepthGauge
from zeroth.core.observability.tracing import configure_tracing
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.policy import PolicyGuard, PolicyRegistry, default_capability_registry
from zeroth.core.secrets import SecretProvider, build_secret_provider
from zeroth.core.service.app import create_app
from zeroth.core.service.auth import JWTBearerTokenVerifier, ServiceAuthConfig, ServiceAuthenticator
from zeroth.core.signing import SigningKeyProvider, build_signing_provider_async
from zeroth.core.storage import AsyncDatabase
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository


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
    # Runtime-managed connector configs (console CRUD; persisted across boots).
    memory_connector_config_repository: MemoryConnectorConfigRepository | None = None
    # Phase 20: Memory resolver for dispatch-time injection.
    memory_resolver: object | None = None
    # Phase 17: Database reference for health probes.
    database: AsyncDatabase | None = None
    # Studio: graph authoring repository (consumed by /api/studio/v1 routes).
    graph_repository: GraphRepository | None = None
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
    # Phase 39: Subgraph composition executor.
    subgraph_executor: object | None = None
    # WS-F: process-wide secret provider (LLM keys, HTTP auth, signing key,
    # execution-unit env). Same instance the entrypoint hands to the runners.
    secret_provider: SecretProvider | None = None
    # WS-D: process-wide provenance signer (deployment attestations + audit
    # chain). None when signing is unconfigured (unsigned-legacy). Threaded into
    # the verify endpoints for the dual (digest + signature) check.
    signer: SigningKeyProvider | None = None
    # WS-E: retention / right-to-erasure surface (per-tenant TTLs, legal holds,
    # full-surface erasure that preserves the audit hash-chain). Always wired;
    # the purge WORKER is only started when ZEROTH_RETENTION__ENABLED is true.
    retention_policy_repository: object | None = None
    legal_hold_repository: object | None = None
    retention_log_repository: object | None = None
    retention_erasure_service: object | None = None
    retention_worker: object | None = None


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
    secret_provider: SecretProvider | None = None,
) -> ServiceBootstrap:
    """Build the service wrapper wiring for a specific deployment."""
    # Phase 43-02 (D-15): wire GraphValidator into GraphRepository so
    # publish() rejects graphs with invalid parallel configs BEFORE the
    # DRAFT -> PUBLISHED state transition.
    _contract_registry = ContractRegistry(database)
    _graph_validator = GraphValidator(contract_registry=_contract_registry)
    graph_repository = GraphRepository(database, validator=_graph_validator)
    deployment_repository = SQLiteDeploymentRepository(database)
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=deployment_repository,
        contract_registry=_contract_registry,
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
    # OBS: enable OpenTelemetry tracing when configured (no-op when disabled).
    configure_tracing(settings.tracing)
    regulus_client: RegulusClient | None = None
    budget_enforcer: object | None = None
    cost_estimator: object | None = None
    if settings.regulus.enabled:
        # Self-auth for calls to the bundled control plane: X-API-Key (Zeroth's
        # own first service key, to pass the gated /regulus mount) + a fresh
        # econ_plane Admin JWT. Degrades gracefully when no Zeroth key exists
        # (Bearer only -> 401 -> fail-open). See econ.service_auth.
        from zeroth.core.econ.service_auth import make_self_auth_headers_provider

        _self_api_key = (
            resolved_auth_config.api_keys[0].secret if resolved_auth_config.api_keys else None
        )
        regulus_self_auth = make_self_auth_headers_provider(_self_api_key)

        regulus_client = RegulusClient(
            base_url=settings.regulus.base_url,
            timeout=settings.regulus.request_timeout,
            enabled=True,
            headers_provider=regulus_self_auth,
        )
        # BudgetEnforcer wired here once econ.budget module lands (Plan 13-02).
        try:
            from zeroth.core.econ.budget import BudgetEnforcer

            # Prefer the bundled in-process mount: a default bundled deploy points
            # base_url at the EXTERNAL localhost:8000 topology, so without this the
            # enforcer would hit a refused socket and silently fail-open — the cap
            # never trips. When econ_plane is importable, dispatch straight to the
            # mounted ASGI app (guarded exactly like the /regulus mount in app.py);
            # otherwise fall back to the external-HTTP base_url path unchanged.
            econ_plane_app = None
            try:
                from zeroth.econ_plane.main import app as econ_plane_app
            except ImportError:
                econ_plane_app = None

            budget_enforcer = BudgetEnforcer(
                regulus_base_url=settings.regulus.base_url if econ_plane_app is None else None,
                cache_ttl=settings.regulus.budget_cache_ttl,
                timeout=settings.regulus.request_timeout,
                headers_provider=regulus_self_auth,
                fail_closed=settings.regulus.fail_closed,
                asgi_app=econ_plane_app,
            )
        except ImportError:
            pass
    # Cost estimation is local (litellm pricing) and needs no Regulus backend, so it
    # is always constructed — cost_usd then populates on every audit record and the
    # local econ lenses (unit economics, waste, right-sizing) work out of the box.
    # Regulus, when enabled above, only adds the cost-event stream and budget caps.
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

    # Runtime-managed connectors: re-register persisted console-authored
    # configs on top of the env-based ones. Bad rows are logged and skipped.
    memory_connector_config_repository = MemoryConnectorConfigRepository(database)
    # WS-B: a deployment is tenant-pinned; load only its tenant's persisted
    # connector configs so another tenant's DSN-bearing rows on a shared DB
    # are never registered into this process.
    await load_persisted_connectors(
        memory_registry,
        memory_connector_config_repository,
        tenant_id=deployment.tenant_id,
    )

    # Phase 20: Create resolver from populated registry for AgentRunner injection.
    memory_resolver = MemoryConnectorResolver(
        registry=memory_registry,
        thread_repository=thread_repository,
    )

    # Phase 20: Wire memory resolver and budget enforcer into orchestrator.
    orchestrator.memory_resolver = memory_resolver
    orchestrator.budget_enforcer = budget_enforcer
    # WS-C: wire the default PolicyGuard so a node's declared capability_bindings
    # become a behavioral, fail-closed gate (agent tool + memory ops, plus the
    # sandbox network/secret gate on executable units). Enforcement is ON by
    # default; ZEROTH_POLICY__ENFORCE_CAPABILITIES=false leaves policy_guard unset
    # (capabilities advisory only). The capability registry resolves every
    # capability by its value, matching the served ref scheme
    # (capability_bindings=["memory_read", ...]). Apps that override
    # orchestrator.policy_guard after bootstrap (e.g. with a bespoke ref scheme)
    # are unaffected — this only sets a default.
    if settings.policy.enforce_capabilities:
        orchestrator.policy_guard = PolicyGuard(
            policy_registry=PolicyRegistry(),
            capability_registry=default_capability_registry(),
        )
    # Local per-run cost ceiling — wired INDEPENDENT of regulus.enabled so the
    # tighter, control-plane-free guard works even without the backend.
    orchestrator.per_run_cap_usd = settings.regulus.per_run_cap_usd

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

    # WS-F: reuse the caller's process-wide secret provider when supplied
    # (entrypoint builds one and threads it in); otherwise build it here from
    # settings so direct callers (tests, bootstrap_app) still get a real
    # provider. Warm only the instance we own to avoid a double prefetch.
    if secret_provider is None:
        secret_provider = build_secret_provider(settings.secrets)
        warm = getattr(secret_provider, "warm", None)
        if callable(warm):
            await warm()

    # WS-D: build the process-wide provenance signer from the SAME shared secret
    # provider (never a second env reader). env -> HMAC when a key resolves;
    # kms -> Ed25519 (raises on missing key); off -> NullSigner. A resolvable-key
    # miss on the dev 'env' path returns None: records stay unsigned-legacy after
    # a loud warning rather than silently minting misleadingly-signed rows.
    import logging as _logging

    signer = await build_signing_provider_async(settings.provenance, secret_provider)
    if signer is None:
        _logging.getLogger(__name__).warning(
            "provenance signing key unresolved for mode=%r; deployment "
            "attestations and audit records will be UNSIGNED-legacy",
            settings.provenance.mode,
        )
    # Inject into the already-constructed service surface (built before the
    # secret provider existed). These are the same references held by the
    # orchestrator / approval service, so post-hoc assignment propagates.
    deployment_service.signer = signer
    audit_repository._signer = signer  # noqa: SLF001 - same-package wiring seam

    # Phase 35: Resilient HTTP client construction — auth secrets resolve
    # through the same provider, not a second env-only one.
    http_client_instance: object | None = None
    http_settings = settings.http_client

    from zeroth.core.http import ResilientHttpClient  # noqa: PLC0415

    http_client_instance = ResilientHttpClient(
        settings=http_settings,
        secret_provider=secret_provider,
    )
    orchestrator.http_client = http_client_instance

    # Phase 36: Template registry and renderer.
    from zeroth.core.templates import TemplateRegistry, TemplateRenderer  # noqa: PLC0415

    template_registry = TemplateRegistry()
    template_renderer = TemplateRenderer()
    orchestrator.template_registry = template_registry
    orchestrator.template_renderer = template_renderer

    # Phase 39: Subgraph composition.
    from zeroth.core.subgraph.executor import SubgraphExecutor  # noqa: PLC0415
    from zeroth.core.subgraph.resolver import SubgraphResolver  # noqa: PLC0415

    subgraph_resolver = SubgraphResolver(deployment_service=deployment_service)
    subgraph_executor = SubgraphExecutor(resolver=subgraph_resolver)
    orchestrator.subgraph_executor = subgraph_executor

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

    # WS-E: retention / right-to-erasure wiring. Repositories + the erasure
    # service are ALWAYS constructed so the API works regardless of the worker;
    # the background purge worker is only built when retention.enabled is True.
    # The econ-event eraser is intentionally left unwired here (None) — see
    # docs/retention-and-erasure.md for the run->join_key deferral.
    from zeroth.core.retention import (
        LegalHoldRepository,
        RetentionAuditLogRepository,
        RetentionErasureService,
        RetentionPolicyRepository,
        RetentionPurgeWorker,
    )

    retention_default_policy = None
    if (
        settings.retention.default_audit_ttl_seconds is not None
        or settings.retention.default_run_ttl_seconds is not None
    ):
        from zeroth.core.retention.models import SYSTEM_DEFAULT_TENANT, RetentionPolicy

        retention_default_policy = RetentionPolicy(
            tenant_id=SYSTEM_DEFAULT_TENANT,
            audit_ttl_seconds=settings.retention.default_audit_ttl_seconds,
            run_ttl_seconds=settings.retention.default_run_ttl_seconds,
        )
    retention_policy_repository = RetentionPolicyRepository(
        database, default_policy=retention_default_policy
    )
    legal_hold_repository = LegalHoldRepository(database)
    retention_log_repository = RetentionAuditLogRepository(database)
    retention_erasure_service = RetentionErasureService(
        audit_repository=audit_repository,
        run_repository=run_repository,
        policy_repository=retention_policy_repository,
        legal_hold_repository=legal_hold_repository,
        log_repository=retention_log_repository,
        artifact_store=artifact_store,
        econ_eraser=None,
    )
    retention_worker_obj: object | None = None
    if settings.retention.enabled:
        retention_worker_obj = RetentionPurgeWorker(
            erasure_service=retention_erasure_service,
            policy_repository=retention_policy_repository,
            poll_interval=settings.retention.worker_poll_interval,
        )

    return ServiceBootstrap(
        database=database,
        graph_repository=graph_repository,
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
        memory_connector_config_repository=memory_connector_config_repository,
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
        subgraph_executor=subgraph_executor,
        secret_provider=secret_provider,
        signer=signer,
        retention_policy_repository=retention_policy_repository,
        legal_hold_repository=legal_hold_repository,
        retention_log_repository=retention_log_repository,
        retention_erasure_service=retention_erasure_service,
        retention_worker=retention_worker_obj,
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
