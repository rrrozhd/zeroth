"""Factory that builds the deployment-scoped service surface."""

from __future__ import annotations

import shutil
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from fastapi import FastAPI

from zeroth.contracts.graph import Graph, GraphRepository, SubgraphNode
from zeroth.contracts.graph.serialization import hydrate_deployed_graph
from zeroth.contracts.graph.versioning import graph_version_ref
from zeroth.contracts.langgraph_gateway.models import CompatibilityResult
from zeroth.contracts.registry import ContractRegistry, contract_scope_context
from zeroth.econ.analytics.client import RegulusClient
from zeroth.governance.approvals import ApprovalRepository, ApprovalService
from zeroth.governance.approvals.notifications import build_approval_notifier
from zeroth.governance.attestations.heartbeat import HeartbeatRepository
from zeroth.governance.attestations.provider import PersistedCapabilityEvidenceProvider
from zeroth.governance.attestations.store import (
    InventoryRegistrationRepository,
    RunAttestationRepository,
)
from zeroth.governance.audit import AuditRepository
from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.governance.decisions.repository import DecisionRepository
from zeroth.governance.decisions.resolvers import (
    DeploymentRecordPolicyResolver,
    PolicyApprovalGate,
    RegisteredInventoryLookup,
)
from zeroth.governance.decisions.service import ToolDecisionService
from zeroth.governance.guardrails.config import GuardrailConfig
from zeroth.governance.guardrails.dead_letter import DeadLetterManager
from zeroth.governance.guardrails.policy import (
    GuardrailPolicyRepository,
    configured_guardrails,
)
from zeroth.governance.guardrails.rate_limit import (
    QuotaEnforcer,
    TokenBucketRateLimiter,
)
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.governance.langgraph_gateway.capabilities import CapabilityReporter
from zeroth.governance.langgraph_gateway.events import AuditGatewayEventSink
from zeroth.governance.policy import (
    Capability,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
    default_capability_registry,
)
from zeroth.integrations.execution import (
    DockerSandboxConfig,
    ExecutableUnitRunner,
    SandboxBackendMode,
    SandboxConfig,
    SandboxManager,
)
from zeroth.integrations.execution.sidecar_client import SandboxSidecarClient
from zeroth.integrations.mcp.config_repository import MCPServerConfigRepository
from zeroth.integrations.memory.config_repository import MemoryConnectorConfigRepository
from zeroth.integrations.memory.factory import register_memory_connectors
from zeroth.integrations.memory.registry import (
    InMemoryConnectorRegistry,
    MemoryConnectorResolver,
)
from zeroth.integrations.memory.runtime_configs import load_persisted_connectors
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
from zeroth.platform.config.settings import SandboxSettings, get_settings
from zeroth.platform.dispatch import LeaseManager
from zeroth.platform.dispatch.operations import SideEffectOperationStore
from zeroth.platform.observability.metrics import MetricsCollector
from zeroth.platform.observability.queue_gauge import QueueDepthGauge
from zeroth.platform.observability.tracing import configure_tracing
from zeroth.platform.secrets import SecretProvider, build_secret_provider
from zeroth.platform.signing import (
    NullSigner,
    build_signing_provider_async,
    build_verification_provider_async,
)
from zeroth.platform.storage import AsyncDatabase, NullWorkspaceScopeContext, ScopeContext
from zeroth.platform.storage.schema_revision import read_async_schema_revision
from zeroth.runtime.agents import AgentRunner
from zeroth.runtime.agents.factory import build_agent_runners
from zeroth.runtime.agents.mcp import RegisteredMCPServerConfig
from zeroth.runtime.agents.provider import ProviderAdapter
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.runtime.orchestration.orchestrator import RuntimeOrchestrator
from zeroth.runtime.orchestration.run_worker import RunWorker
from zeroth.service.api.authentication import (
    JWTBearerTokenVerifier,
    ServiceAuthConfig,
    ServiceAuthenticator,
)
from zeroth.service.api.authorization import RoleRegistry
from zeroth.service.api.health import SERVICE_MIGRATIONS_PACKAGE
from zeroth.service.app import create_app
from zeroth.service.bootstrap.admission import BoundAdmissionEvaluator
from zeroth.service.bootstrap.container import (
    DeploymentBootstrapError,
    ServiceBootstrap,
)
from zeroth.service.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.service.langgraph_gateway.compatibility import CompatibilityDetector
from zeroth.service.langgraph_gateway.context import ReservedContextCodec
from zeroth.service.langgraph_gateway.enforcement import (
    LangGraphEnforcementRepository,
    LangGraphEnforcementService,
    StoredCapabilityEvidenceProvider,
)
from zeroth.service.langgraph_gateway.proxy import GatewayProxy
from zeroth.service.langgraph_gateway.routes import WebSocketGatewayHandler
from zeroth.service.langgraph_gateway.transport import HTTPGatewayTransport


class _UnavailableEconEventEraser:
    """Fail a configured economics cleanup instead of marking it skipped."""

    async def delete_events_for_run(
        self,
        tenant_id: str,
        join_keys: object,
        *,
        idempotency_key: str,
    ) -> int:
        del tenant_id, join_keys, idempotency_key
        raise RuntimeError("economics erasure unavailable")


def _build_retention_econ_eraser(settings: object) -> object | None:
    """Bind retention cleanup to the configured bundled economics database.

    A disabled control plane keeps the compatibility behavior: no econ cleanup
    is expected, so the manifest records that surface as skipped. Once economics
    is enabled, however, an absent adapter is not equivalent to an empty store.
    The unavailable stand-in makes the destructive operation durably fail and
    remain retryable instead of reporting a false successful erasure.
    """
    regulus = getattr(settings, "regulus", None)
    if not bool(getattr(regulus, "enabled", False)):
        return None
    try:
        from zeroth.econ.plane.database import SessionLocal as EconSessionLocal
        from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser
    except ImportError:
        return _UnavailableEconEventEraser()
    return SqlAlchemyEconEventEraser(session_factory=EconSessionLocal)


def _configured_policy_registry(
    definitions: tuple[dict[str, Any], ...],
) -> PolicyRegistry:
    """Populate the registry the guard resolves policy bindings against.

    `LangGraphGatewaySettings.policy_bindings` already names the policies a governed
    request is evaluated against, but nothing ever put a definition into the registry
    those refs resolve through — so a deployment naming a binding was refused with
    `zeroth.policy_unavailable` on every governed request, and one naming none admitted
    everything. The deny path existed and could not be reached either way.

    Validated here rather than at settings-parse time so a malformed policy fails the
    deployment loudly instead of degrading it into one that admits traffic silently.
    """
    registry = PolicyRegistry()
    for definition in definitions:
        registry.register(PolicyDefinition.model_validate(definition))
    return registry


def _build_sandbox_manager(cfg: SandboxSettings) -> SandboxManager:
    """Build a SandboxManager honouring the configured sandbox backend.

    Under default settings (backend='local') the produced config is byte-equal to
    a bare ``SandboxConfig()``, so existing default-settings behaviour is
    unchanged. The sidecar client is constructed LAZILY, only for backend=sidecar
    (its __init__ fail-closes when ZEROTH_SANDBOX_SIDECAR_SECRET is unset — the
    intended boot error for a sidecar deployment, not a regression for
    local/docker). ``sidecar_url`` is set only in the sidecar branch so a
    non-sidecar config stays identical to the default.
    """
    backend = SandboxBackendMode(cfg.backend)
    docker = DockerSandboxConfig(
        container_name=cfg.docker_container_name,
        docker_binary=cfg.docker_binary,
    )
    sidecar_client = None
    sidecar_url = None
    if backend is SandboxBackendMode.SIDECAR:
        sidecar_url = cfg.sidecar_url
        sidecar_client = SandboxSidecarClient(cfg.sidecar_url)
    config = SandboxConfig(backend=backend, docker=docker, sidecar_url=sidecar_url)
    return SandboxManager(config=config, sidecar_client=sidecar_client)


async def bootstrap_scoped_service(
    database: AsyncDatabase,
    *,
    deployment_ref: str,
    tenant_id: str = "default",
    workspace_id: str | None = None,
    agent_runners: Mapping[str, AgentRunner] | None = None,
    executable_unit_runner: ExecutableUnitRunner | None = None,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
    guardrail_config: GuardrailConfig | None = None,
    enable_durable_worker: bool = True,
    secret_provider: SecretProvider | None = None,
) -> ServiceBootstrap:
    """Build the service wrapper wiring for a specific deployment."""
    deployment_repository = SQLiteDeploymentRepository(database)
    deployment = await deployment_repository.get(
        deployment_ref,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    if deployment is None:
        raise DeploymentBootstrapError(f"deployment {deployment_ref!r} not found")

    # The persisted deployment is the trusted bootstrap boundary: it fixes the
    # tenant whose contracts this deployment may resolve before any registry
    # or graph validator is constructed.
    _contract_registry = ContractRegistry.scoped(
        database,
        contract_scope_context(deployment.tenant_id, deployment.workspace_id),
    )
    # The operator-owned ceiling for mcp_tool nodes. Without this resolver the
    # validator's grants pass returns immediately and publish accepts any
    # capability an author declares -- the check exists in exactly one place,
    # so leaving it unwired made the whole registry decorative rather than
    # merely unenforced.
    _mcp_server_configs = MCPServerConfigRepository(database)

    async def _resolve_mcp_grants(server_ref: str) -> set[Capability] | None:
        record = await _mcp_server_configs.get(server_ref, tenant_id=deployment.tenant_id)
        return None if record is None else set(record.grants)

    _graph_validator = GraphValidator(
        contract_registry=_contract_registry,
        mcp_grants_resolver=_resolve_mcp_grants,
    )
    from zeroth.service.templates import TemplateReferenceIndex  # noqa: PLC0415

    template_reference_index = TemplateReferenceIndex(
        database,
        tenant_id=deployment.tenant_id,
        workspace_id=deployment.workspace_id,
    )
    deployment_repository = SQLiteDeploymentRepository(
        database,
        template_reference_index=template_reference_index,
    )
    graph_repository = GraphRepository(
        database,
        validator=_graph_validator,
        template_reference_index=template_reference_index,
    )
    # Reconcile the dependency index only against a schema that actually has it.
    # rebuild() deletes and re-derives rows in template_dependency_references,
    # which migration 032 introduced, so on a database still behind that revision
    # it raised "no such table" and turned a stale-schema candidate into a
    # bootstrap crash instead of the clean rejection the caller is looking for.
    # A stale database has nothing worth reconciling anyway.
    schema = await read_async_schema_revision(database, SERVICE_MIGRATIONS_PACKAGE)
    if schema.state == "current":
        await template_reference_index.rebuild()
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=deployment_repository,
        contract_registry=_contract_registry,
    )
    try:
        graph = hydrate_deployed_graph(deployment)
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

    deployment_scope = contract_scope_context(deployment.tenant_id, deployment.workspace_id)
    run_repository = RunRepository(database, deployment_scope)
    thread_repository = ThreadRepository(database, deployment_scope)
    audit_repository = AuditRepository.scoped(
        database,
        contract_scope_context(deployment.tenant_id, deployment.workspace_id),
    )
    approval_repository = ApprovalRepository.scoped_for_deployment(
        database, deployment_scope, deployment.deployment_ref
    )
    approval_service = ApprovalService(
        repository=approval_repository,
        run_repository=run_repository,
        audit_repository=audit_repository,
    )
    contract_registry = deployment_service.contract_registry
    resolved_agent_runners = dict(agent_runners or {})
    # Resolved here (memoized singleton) so the default executable-unit runner
    # actually honours settings.sandbox instead of always running untrusted code
    # on the bare LOCAL backend. An injected runner is left untouched.
    settings = get_settings()
    resolved_executable_unit_runner = executable_unit_runner or ExecutableUnitRunner(
        sandbox_manager=_build_sandbox_manager(settings.sandbox),
    )
    metrics_collector = MetricsCollector()
    # ZER-26: the durable receipt store is what turns the runtime's at-least-once
    # boundary into a recognisable repeat. Constructed here so live executions get
    # replay suppression and operation metrics -- the dispatch path is a
    # pass-through whenever this is absent.
    operation_store = SideEffectOperationStore(
        database,
        deployment_scope,
        metrics_collector=metrics_collector,
    )
    async def _resolve_mcp_server(server_ref: str) -> RegisteredMCPServerConfig | None:
        """Turn a graph's server_ref into the operator's registration.

        The graph author writes only the ref; the command, args, env and grants
        all come from this row, which they cannot edit. Returning None lets the
        pool report an unregistered server by name instead of failing somewhere
        further in.
        """
        record = await _mcp_server_configs.get(server_ref, tenant_id=deployment.tenant_id)
        if record is None:
            return None
        return RegisteredMCPServerConfig(
            name=record.ref,
            command=record.command,
            args=list(record.args),
            env=dict(record.env) or None,
            grants=list(record.grants),
        )

    orchestrator = RuntimeOrchestrator(
        run_repository=run_repository,
        agent_runners=resolved_agent_runners,
        executable_unit_runner=resolved_executable_unit_runner,
        audit_repository=audit_repository,
        approval_service=approval_service,
        operation_store=operation_store,
        mcp_server_resolver=_resolve_mcp_server,
    ).use_token_snapshot_store(run_repository)
    resolved_auth_config = auth_config or ServiceAuthConfig.from_env()
    authenticator = ServiceAuthenticator(
        resolved_auth_config,
        bearer_verifier=bearer_token_verifier,
    )
    role_registry = RoleRegistry.from_config(resolved_auth_config.custom_roles)

    # Phase 9: durable dispatch, guardrails, observability.
    resolved_guardrail_config = guardrail_config or GuardrailConfig()
    lease_manager = LeaseManager(database)
    dead_letter_manager = DeadLetterManager(
        run_repository=run_repository,
        max_failure_count=resolved_guardrail_config.max_failure_count,
    )
    guardrail_scope = (
        NullWorkspaceScopeContext.for_default_compatibility()
        if deployment.tenant_id == "default"
        else NullWorkspaceScopeContext(tenant_id=deployment.tenant_id)
    )
    rate_limiter = TokenBucketRateLimiter.scoped(database, guardrail_scope)
    quota_enforcer = QuotaEnforcer.scoped(database, guardrail_scope)
    guardrail_policy_repository = GuardrailPolicyRepository.scoped(
        database,
        guardrail_scope,
        baseline=configured_guardrails(resolved_guardrail_config),
    )
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
            tenant_id=deployment.tenant_id,
            workspace_id=deployment.workspace_id,
            max_concurrency=resolved_guardrail_config.max_concurrency,
            dead_letter_manager=dead_letter_manager,
            metrics_collector=metrics_collector,
        )
        worker.guardrail_policy_repository = guardrail_policy_repository

    # Phase 13: Regulus economics integration.
    approval_service.notifier = build_approval_notifier(settings.approval_notifications)
    # OBS: enable OpenTelemetry tracing when configured (no-op when disabled).
    configure_tracing(settings.tracing)
    regulus_client: RegulusClient | None = None
    budget_enforcer: object | None = None
    cost_estimator: object | None = None
    # Self-auth + in-process mount resolved ONCE, before the regulus/gateway
    # branches: X-API-Key (Zeroth's own first service key, to pass the gated
    # /regulus mount) + a fresh econ_plane Admin JWT (per-tenant, minted at call
    # time). A default bundled deploy points regulus.base_url at the EXTERNAL
    # localhost:8000 topology, but the plane is mounted at /regulus on this
    # service's own port, so the cost-event WRITE path (RegulusClient), the budget
    # READ path (BudgetEnforcer), AND the gateway-only fallback enforcer must all
    # dispatch straight into the mounted ASGI app; otherwise writes POST to a
    # refused socket (spend stays 0, caps never trip) and reads/gateway checks hit
    # an unauthenticated external socket and silently fail open. Degrades
    # gracefully when no Zeroth key exists (Bearer only -> 401 -> fail-open).
    # See econ.service_auth.
    from zeroth.econ.analytics.service_auth import make_self_auth_headers_provider

    _self_api_key = (
        resolved_auth_config.api_keys[0].secret if resolved_auth_config.api_keys else None
    )
    regulus_self_auth = make_self_auth_headers_provider(_self_api_key)
    econ_plane_app = None
    try:
        from zeroth.econ.plane.main import app as econ_plane_app
    except ImportError:
        econ_plane_app = None

    if settings.regulus.enabled:
        regulus_client = RegulusClient(
            # Keep base_url the operator-configured, RESOLVABLE value even when
            # dispatching in-process: httpx.ASGITransport routes the cost-event POST
            # by PATH (the host is ignored), so asgi_app alone lands events in the
            # mounted plane. The readiness probe HTTP-GETs regulus_client.base_url,
            # so it must stay a real host — pointing it at the unroutable
            # 'regulus.internal' flips /health/ready to degraded (regression).
            base_url=settings.regulus.base_url,
            timeout=settings.regulus.request_timeout,
            enabled=True,
            headers_provider=regulus_self_auth,
            _asgi_app=econ_plane_app,
        )
        # BudgetEnforcer wired here once econ.budget module lands (Plan 13-02).
        try:
            from zeroth.econ.analytics.budget import BudgetEnforcer

            # Prefer the bundled in-process mount: a default bundled deploy points
            # base_url at the EXTERNAL localhost:8000 topology, so without this the
            # enforcer would hit a refused socket and silently fail-open — the cap
            # never trips. When econ_plane is importable, dispatch straight to the
            # mounted ASGI app (guarded exactly like the /regulus mount in app.py);
            # otherwise fall back to the external-HTTP base_url path unchanged.
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
        from zeroth.econ.analytics.cost import CostEstimator

        cost_estimator = CostEstimator()
    except ImportError:
        cost_estimator = None

    probe_instrumentation = None
    if regulus_client is not None:
        # Explicitly costed provider/connector probes share the bundled econ
        # store with the mounted Regulus app, so admission is durable before the
        # external call and the same event identity reaches Audit and Regulus.
        from zeroth.econ.plane.database import SessionLocal as EconSessionLocal
        from zeroth.service.probe_instrumentation import PersistentProbeInstrumentation

        probe_instrumentation = PersistentProbeInstrumentation(
            session_factory=EconSessionLocal,
            regulus_client=regulus_client,
            audit_repository=audit_repository,
            deployment_ref=deployment.deployment_ref,
            graph_version_ref=f"{graph.graph_id}@{graph.version}",
            workspace_id=deployment.workspace_id,
        )

    # Phase 18: Wire cost instrumentation into orchestrator.
    orchestrator.regulus_client = regulus_client
    orchestrator.cost_estimator = cost_estimator
    orchestrator.cost_instrumentation = probe_instrumentation
    orchestrator.deployment_ref = deployment.deployment_ref

    # Phase 16/18: ARQ wakeup pool.
    arq_pool = None
    if settings.dispatch.arq_enabled:
        try:
            from zeroth.platform.dispatch.arq_wakeup import create_arq_pool

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
        memory_registry,
        settings,
        redis_client=redis_client,
        pg_conninfo=pg_conninfo,
        secret_provider=secret_provider,
        tenant_id=deployment.tenant_id,
        allow_env_fallback=settings.secrets.allow_env_fallback,
    )

    # Runtime-managed connectors: re-register persisted console-authored
    # configs on top of the env-based ones. Bad rows are logged and skipped.
    memory_connector_config_repository = MemoryConnectorConfigRepository(database)
    # Operator-registered MCP servers. Nothing is spawned at bootstrap: a
    # server starts lazily on the first tool call that needs it. Same instance
    # the publish-time grants resolver reads, so the admin API and the ceiling
    # can never disagree about what is registered.
    mcp_server_config_repository = _mcp_server_configs
    # WS-B: a deployment is tenant-pinned; load only its tenant's persisted
    # connector configs so another tenant's DSN-bearing rows on a shared DB
    # are never registered into this process.
    await load_persisted_connectors(
        memory_registry,
        memory_connector_config_repository,
        tenant_id=deployment.tenant_id,
        secret_provider=secret_provider,
        allow_env_fallback=settings.secrets.allow_env_fallback,
    )

    # Phase 20: Create resolver from populated registry for AgentRunner injection.
    memory_resolver = MemoryConnectorResolver(
        registry=memory_registry,
        thread_repository=thread_repository,
    )
    if probe_instrumentation is not None and cost_estimator is not None:
        from zeroth.service.probe_instrumentation import PersistentEmbeddingInstrumentation

        run_cap = settings.regulus.per_run_cap_usd
        if run_cap is not None:
            memory_resolver.set_embedding_call_hooks(
                PersistentEmbeddingInstrumentation(
                    instrumentation=probe_instrumentation,
                    cost_estimator=cost_estimator,
                    run_cap_usd=Decimal(str(run_cap)),
                )
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
    policy_guard = PolicyGuard(
        policy_registry=_configured_policy_registry(settings.policy.definitions),
        capability_registry=default_capability_registry(),
    )
    if settings.policy.enforce_capabilities:
        orchestrator.policy_guard = policy_guard
    # Local per-run cost ceiling — wired INDEPENDENT of regulus.enabled so the
    # tighter, control-plane-free guard works even without the backend.
    orchestrator.per_run_cap_usd = settings.regulus.per_run_cap_usd

    # Phase 34: Artifact store construction and wiring.
    artifact_store: object | None = None
    artifact_settings = settings.artifact_store
    if artifact_settings.backend == "filesystem":
        from zeroth.platform.artifacts.store import FilesystemArtifactStore

        artifact_store = FilesystemArtifactStore(
            base_dir=artifact_settings.filesystem_base_dir,
            default_ttl=artifact_settings.default_ttl_seconds,
            max_size=artifact_settings.max_artifact_size_bytes,
        )
    elif artifact_settings.backend == "redis" and redis_client is not None:
        from zeroth.platform.artifacts.store import RedisArtifactStore

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
    artifact_backend = artifact_store
    if artifact_backend is not None:
        from zeroth.platform.artifacts.tenant_scoped import TenantScopedArtifactStore

        artifact_store = TenantScopedArtifactStore(
            artifact_backend,
            tenant_id=deployment.tenant_id,
            workspace_id=deployment.workspace_id,
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
    # The verify side is built independently: it retains keys this deployment has
    # rotated away from, and survives signing being switched off, so rows signed
    # earlier stay verifiable. Absent only when no key material exists at all.
    verifier = await build_verification_provider_async(settings.provenance, secret_provider)
    from zeroth.service.certifications.models import ServingArtifactIdentity
    from zeroth.service.certifications.repository import CertificationRepository
    from zeroth.service.certifications.service import CertificationService

    certification_settings = settings.certification
    serving_artifact_identity = (
        ServingArtifactIdentity(
            target_key=deployment.deployment_ref,
            app_commit=certification_settings.serving_app_commit,
            image_digest=certification_settings.serving_image_digest,
        )
        if certification_settings.serving_app_commit and certification_settings.serving_image_digest
        else None
    )
    certification_service = CertificationService(
        CertificationRepository(database),
        verifier=verifier,
        metrics=metrics_collector,
    )
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
    deployment_service.deployment_mode = settings.deployment_mode
    audit_repository._signer = signer  # noqa: SLF001 - same-package wiring seam

    # Phase 35: Resilient HTTP client construction — auth secrets resolve
    # through the same provider, not a second env-only one.
    http_client_instance: object | None = None
    http_settings = settings.http_client

    from zeroth.integrations.http import ResilientHttpClient  # noqa: PLC0415

    http_client_instance = ResilientHttpClient(
        settings=http_settings,
        secret_provider=secret_provider,
    )
    orchestrator.http_client = http_client_instance

    # Phase 36: Template registry and renderer.
    from zeroth.contracts.templates import TemplateRenderer  # noqa: PLC0415
    from zeroth.service.templates import (  # noqa: PLC0415
        DatabaseTemplateRegistry,
        TemplateDependencyChecker,
    )

    template_registry = DatabaseTemplateRegistry(
        database,
        tenant_id=deployment.tenant_id,
        workspace_id=deployment.workspace_id,
    )
    template_dependency_checker = TemplateDependencyChecker(
        graph_repository,
        deployment_service,
        template_reference_index,
    )
    template_renderer = TemplateRenderer()
    orchestrator.template_registry = template_registry
    orchestrator.template_renderer = template_renderer

    # Phase 39: Subgraph composition.
    from zeroth.runtime.subgraphs.executor import SubgraphExecutor  # noqa: PLC0415
    from zeroth.runtime.subgraphs.resolver import SubgraphResolver  # noqa: PLC0415

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
            from zeroth.service.service_audit import ServiceAuditRecorder
            from zeroth.service.webhooks.repository import WebhookRepository
            from zeroth.service.webhooks.service import WebhookService

            webhook_repository = WebhookRepository(database, deployment_scope)
            webhook_audit_recorder = ServiceAuditRecorder(
                repository=audit_repository,
                deployment=deployment,
                require_signed=signer is not None and not isinstance(signer, NullSigner),
            )
            webhook_service_obj = WebhookService(
                repository=webhook_repository,
                default_max_retries=settings.webhook.default_max_retries,
                audit_recorder=webhook_audit_recorder,
            )
            # Wire webhook_service into orchestrator and approval_service
            orchestrator.webhook_service = webhook_service_obj
            approval_service.webhook_service = webhook_service_obj

            import httpx

            from zeroth.service.webhooks.delivery import WebhookDeliveryWorker

            webhook_http_client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                timeout=httpx.Timeout(settings.webhook.delivery_timeout),
            )
            delivery_worker_obj = WebhookDeliveryWorker(
                repository=webhook_repository,
                http_client=webhook_http_client,
                audit_recorder=webhook_audit_recorder,
                poll_interval=settings.webhook.delivery_poll_interval,
                max_concurrency=settings.webhook.max_delivery_concurrency,
                retry_base_delay=settings.webhook.retry_base_delay,
                retry_max_delay=settings.webhook.retry_max_delay,
            )
        except ImportError:
            pass

    if settings.approval_sla.enabled:
        try:
            from zeroth.governance.approvals.sla_checker import ApprovalSLAChecker

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
    # When the bundled economics plane is active, erase its tenant-scoped events
    # with the authoritative run/join keys harvested from the audit records.
    from zeroth.governance.retention import (
        EnabledPolicyMaintenanceReader,
        LegalHoldRepository,
        RetentionAuditLogRepository,
        RetentionErasureService,
        RetentionOwnerMaintenanceReader,
        RetentionPolicyRepository,
        RetentionPurgeWorker,
        RetentionWorkspaceMaintenanceReader,
    )

    retention_default_policy = None
    if (
        settings.retention.default_audit_ttl_seconds is not None
        or settings.retention.default_run_ttl_seconds is not None
    ):
        from zeroth.governance.retention.models import (
            SYSTEM_DEFAULT_TENANT,
            RetentionPolicy,
        )

        retention_default_policy = RetentionPolicy(
            tenant_id=SYSTEM_DEFAULT_TENANT,
            audit_ttl_seconds=settings.retention.default_audit_ttl_seconds,
            run_ttl_seconds=settings.retention.default_run_ttl_seconds,
        )
    retention_scope = (
        NullWorkspaceScopeContext.for_default_compatibility()
        if deployment.tenant_id == "default"
        else NullWorkspaceScopeContext(tenant_id=deployment.tenant_id)
    )
    retention_policy_repository = RetentionPolicyRepository.scoped(
        database, retention_scope, default_policy=retention_default_policy
    )
    legal_hold_repository = LegalHoldRepository(database, retention_scope)
    retention_log_repository = RetentionAuditLogRepository(database, retention_scope)
    retention_econ_eraser = _build_retention_econ_eraser(settings)
    retention_erasure_service = RetentionErasureService(
        audit_repository=audit_repository,
        run_repository=run_repository,
        policy_repository=retention_policy_repository,
        legal_hold_repository=legal_hold_repository,
        log_repository=retention_log_repository,
        artifact_store=artifact_store,
        econ_eraser=retention_econ_eraser,
    )
    retention_worker_obj: object | None = None
    if settings.retention.enabled:

        def policy_repository_for(tenant_id: str) -> RetentionPolicyRepository:
            scope = (
                NullWorkspaceScopeContext.for_default_compatibility()
                if tenant_id == "default"
                else NullWorkspaceScopeContext(tenant_id=tenant_id)
            )
            return RetentionPolicyRepository.scoped(
                database, scope, default_policy=retention_default_policy
            )

        def retention_service_for(
            tenant_scope: ScopeContext | NullWorkspaceScopeContext,
        ) -> RetentionErasureService:
            tenant_id = tenant_scope.tenant_id
            workspace_id = tenant_scope.workspace_id if type(tenant_scope) is ScopeContext else None
            null_scope = (
                NullWorkspaceScopeContext.for_default_compatibility()
                if tenant_id == "default"
                else NullWorkspaceScopeContext(tenant_id=tenant_id)
            )
            tenant_artifacts = None
            if artifact_backend is not None:
                from zeroth.platform.artifacts.tenant_scoped import TenantScopedArtifactStore

                tenant_artifacts = TenantScopedArtifactStore(
                    artifact_backend, tenant_id=tenant_id, workspace_id=workspace_id
                )
            return RetentionErasureService(
                audit_repository=AuditRepository.scoped(database, tenant_scope, signer),
                run_repository=RunRepository(database, tenant_scope),
                policy_repository=policy_repository_for(tenant_id),
                legal_hold_repository=LegalHoldRepository(database, null_scope),
                log_repository=RetentionAuditLogRepository(database, null_scope),
                artifact_store=tenant_artifacts,
                econ_eraser=retention_econ_eraser,
            )

        retention_worker_obj = RetentionPurgeWorker.for_shared_database(
            policy_reader=EnabledPolicyMaintenanceReader(database),
            tenant_reader=RetentionOwnerMaintenanceReader(database),
            policy_repository_factory=policy_repository_for,
            workspace_reader_factory=lambda tenant_id: RetentionWorkspaceMaintenanceReader(
                database, tenant_id
            ),
            erasure_service_factory=retention_service_for,
            poll_interval=settings.retention.worker_poll_interval,
        )

    # ZER-37: the GitHub App integration surface, constructed only when
    # settings.github.enabled is true. When disabled nothing is built and the
    # webhook route is never registered (the path 404s like any other unknown
    # route). Enabled without the git binary fails the deployment loudly at
    # bootstrap instead of failing the first checkout at run time.
    github_repository: object | None = None
    github_client: object | None = None
    github_token_broker: object | None = None
    github_integration_service: object | None = None
    github_maintenance_worker: object | None = None
    github_webhook_secret_resolver: object | None = None
    repo_checkout_repository: object | None = None
    repo_run_repository: object | None = None
    repository_unit_service: object | None = None
    repo_run_worker: object | None = None
    if settings.github.enabled:
        if shutil.which("git") is None:
            raise DeploymentBootstrapError(
                "GitHub integration is enabled but the 'git' binary is not on PATH"
            )
        import tempfile
        from pathlib import Path

        from zeroth.contracts.repo_manifest import RepoUnitPolicy
        from zeroth.integrations.execution.integrity import AdmissionController
        from zeroth.integrations.github.app_jwt import AppJwtIssuer
        from zeroth.integrations.github.checkout import CheckoutService
        from zeroth.integrations.github.client import GitHubAppClient
        from zeroth.integrations.github.config import GitHubAppConfig
        from zeroth.integrations.github.git_cli import GitInvocation
        from zeroth.integrations.github.token_broker import InstallationTokenBroker
        from zeroth.platform.secrets.provider import resolve_secret_async
        from zeroth.service.github.janitor import GitHubMaintenanceWorker
        from zeroth.service.github.repository import SQLiteGitHubRepository
        from zeroth.service.github.service import GitHubIntegrationService
        from zeroth.service.repositories.repository import (
            SQLiteRepoCheckoutRepository,
            SQLiteRepoRunRepository,
        )
        from zeroth.service.repositories.service import (
            RepoCheckoutPipelineRecorder,
            RepositoryUnitService,
        )
        from zeroth.service.repositories.worker import RepoRunWorker

        github_config = GitHubAppConfig(
            app_id=settings.github.app_id,
            api_base_url=settings.github.api_base_url,
            git_base_url=settings.github.git_base_url,
            private_key_secret_name=settings.github.private_key_secret_name,
            max_file_bytes=settings.github.max_file_bytes,
            max_total_bytes=settings.github.max_total_bytes,
            max_file_count=settings.github.max_file_count,
        )
        github_jwt_issuer = AppJwtIssuer(
            github_config, secret_provider, tenant_id=deployment.tenant_id
        )
        github_client = GitHubAppClient(github_config, github_jwt_issuer)
        github_token_broker = InstallationTokenBroker(github_client)
        github_repository = SQLiteGitHubRepository(database)
        github_integration_service = GitHubIntegrationService(
            github_repository,
            github_client,
            github_token_broker,
            config=github_config,
            jwt_issuer=github_jwt_issuer,
            tenant_id=deployment.tenant_id,
        )
        # ZER-37 orchestration glue: checkout staging, repo-run persistence,
        # the repository-unit service, and the repo-run execution worker. The
        # staging root defaults to a "stages" sibling of the resolved cache
        # directory; directories are created lazily at first use, not here.
        github_cache_root = (
            Path(settings.github.cache_dir)
            if settings.github.cache_dir
            else Path(tempfile.gettempdir()) / "zeroth-github" / "cache"
        )
        github_staging_root = (
            Path(settings.github.staging_dir)
            if settings.github.staging_dir
            else github_cache_root.parent / "stages"
        )
        repo_checkout_repository = SQLiteRepoCheckoutRepository(database)
        repo_run_repository = SQLiteRepoRunRepository(database)
        # One admission controller shared by the staging service (which
        # registers each checkout's trusted manifest digest) and the run
        # worker's per-run runners (which enforce it). Repository units are
        # python3-only in v1, so the runtime/command allowlists pin that.
        repo_admission_controller = AdmissionController(
            allowed_runtimes={"python"}, allowed_commands={"python3"}
        )
        repo_policy = RepoUnitPolicy()
        github_checkout_service = CheckoutService(
            github_config,
            github_client,
            github_token_broker,
            GitInvocation(),
            cache_dir=github_cache_root,
            store=RepoCheckoutPipelineRecorder(repo_checkout_repository),
        )
        repository_unit_service = RepositoryUnitService(
            checkout_repository=repo_checkout_repository,
            run_repository=repo_run_repository,
            github_repository=github_repository,
            checkout_service=github_checkout_service,
            admission_controller=repo_admission_controller,
            policy=repo_policy,
            staging_root=github_staging_root,
            signer=signer,
            checkout_ttl_seconds=settings.github.checkout_ttl_seconds,
        )
        # The worker shares the bootstrap runner's SandboxManager (so repo
        # runs honour settings.sandbox) but builds a private per-run
        # ExecutableUnitRunner around it, because the checkout-materializer
        # seam is per-run state.
        repo_run_worker = RepoRunWorker(
            checkout_repository=repo_checkout_repository,
            run_repository=repo_run_repository,
            github_repository=github_repository,
            audit_repository=audit_repository,
            policy=repo_policy,
            sandbox_manager=resolved_executable_unit_runner.sandbox_manager,
            admission_controller=repo_admission_controller,
            deployment_ref=deployment.deployment_ref,
            tenant_id=deployment.tenant_id,
            workspace_id=deployment.workspace_id,
            poll_interval=settings.github.repo_run_poll_seconds,
        )
        github_maintenance_worker = GitHubMaintenanceWorker(
            github_repository,
            tenant_id=deployment.tenant_id,
            checkout_repository=repo_checkout_repository,
            staging_root=github_staging_root,
            workspace_id=deployment.workspace_id,
        )
        github_secret_name = settings.github.webhook_secret_name
        github_tenant_id = deployment.tenant_id
        github_secret_provider = secret_provider

        async def _resolve_github_webhook_secret() -> str | None:
            """Resolve the webhook secret through the shared secret provider."""
            return await resolve_secret_async(
                github_secret_provider, github_secret_name, tenant_id=github_tenant_id
            )

        github_webhook_secret_resolver = _resolve_github_webhook_secret

    # ZER-8: the tool-enforcement surface. Wired unconditionally -- an SDK
    # adapter that cannot reach a decision endpoint falls back to its own
    # deny-everything default, so leaving this behind a flag would make the
    # governed path silently unusable rather than safely off.
    decision_repository = DecisionRepository(database)
    # ``budget_enforcer`` is ``None`` whenever the economics backend is not
    # configured. That is deliberately not patched over here: ``admit`` turns an
    # unreachable budget checker into ``zeroth.budget_unavailable``, which the
    # decision service records as a denial. Fail-closed is the intended posture
    # (see the module docstring of zeroth.governance.decisions.service), and
    # substituting a permissive stand-in would be the one change that breaks it.
    inventory_registration_repository = InventoryRegistrationRepository(database)
    # The two facts a decision must not take from its caller (audit round 1):
    # which tool is being called, and which policies apply. Both are read from
    # server-held state -- the deployment's registered inventory, and the
    # deployment record's own policy bindings. Passed unconditionally: a branch
    # here would add a decision point to a function already at the complexity
    # ceiling, and an unwired resolver is exactly the state the audit found.
    tool_decision_service = ToolDecisionService(
        repository=decision_repository,
        # The admission combiner is injected rather than imported by the
        # decision service (ZER-24): governance may not depend on the
        # service-classified gateway package that owns ``admit``.
        admission_evaluator=BoundAdmissionEvaluator(
            policy_guard=policy_guard,
            budget_checker=budget_enforcer,
        ),
        inventory=RegisteredInventoryLookup(inventory_registration_repository),
        deployment_policies=DeploymentRecordPolicyResolver(
            deployment_service.get,
            workspace_id=deployment.workspace_id,
        ),
        # Without a gate the service defaults to ``NoApprovalRequired``,
        # which answers "no hold" for every call -- so a policy carrying
        # ``approval_required_for_side_effects`` was silently ignored for
        # tool calls while being honoured everywhere else.
        # The guard's OWN registry, so the gate and
        # ``evaluate_run_admission`` cannot disagree about which policy a
        # binding names.
        approval_gate=PolicyApprovalGate(policy_guard.policy_registry),
    )
    run_attestation_repository = RunAttestationRepository(database)
    enforcement_heartbeat_repository = HeartbeatRepository(database)

    gateway_proxy: object | None = None
    gateway_transport: HTTPGatewayTransport | None = None
    gateway_compatibility: CompatibilityResult | None = None
    gateway_capability_reporter: object | None = None
    langgraph_enforcement_service: object | None = None
    gateway_websocket_handler: object | None = None
    audit_delivery_queue: AuditDeliveryQueue | None = None
    gateway_settings = settings.langgraph_gateway
    if gateway_settings.enabled:
        if signer is None or isinstance(signer, NullSigner):
            raise DeploymentBootstrapError(
                "LangGraph gateway requires an available provenance signer"
            )
        if budget_enforcer is None:
            from zeroth.econ.analytics import BudgetEnforcer

            # Gateway-only fallback: authenticate through the mounted plane in-process,
            # exactly like the regulus-enabled path above. Built bare (no
            # headers_provider, external base_url) it could never authenticate, so
            # the gateway's budget check silently failed open.
            budget_enforcer = BudgetEnforcer(
                regulus_base_url=(
                    settings.regulus.base_url if econ_plane_app is None else None
                ),
                cache_ttl=settings.regulus.budget_cache_ttl,
                timeout=settings.regulus.request_timeout,
                headers_provider=regulus_self_auth,
                fail_closed=settings.regulus.fail_closed,
                asgi_app=econ_plane_app,
            )
            orchestrator.budget_enforcer = budget_enforcer

        context_codec = ReservedContextCodec(
            signer,
            max_ttl_seconds=gateway_settings.context_ttl_seconds,
        )
        gateway_transport = HTTPGatewayTransport(gateway_settings, secret_provider)
        try:
            # Reuse the transport's sole long-lived client for the one bounded
            # compatibility probe instead of allocating a second client.
            gateway_transport.client.base_url = gateway_settings.upstream_url
            detector = CompatibilityDetector(
                gateway_transport.client,
                tested_langgraph_versions=gateway_settings.supported_langgraph_versions,
                tested_agent_server_versions=gateway_settings.supported_agent_server_versions,
                timeout_seconds=gateway_settings.connect_timeout_seconds,
            )
            gateway_compatibility = await detector.detect()
            langgraph_enforcement_repository = LangGraphEnforcementRepository(
                database, deployment_scope
            )
            langgraph_enforcement_service = LangGraphEnforcementService(
                langgraph_enforcement_repository,
                codec=context_codec,
                signer=signer,
                policy_guard=policy_guard,
                budget_checker=budget_enforcer,
                metrics=metrics_collector,
                deployment_ref=str(gateway_settings.deployment_ref),
                audience=str(gateway_settings.upstream_audience),
                expected_graph_version=deployment.graph_version_ref,
                policy_bindings=gateway_settings.policy_bindings,
                expected_inventory_fingerprint=(
                    gateway_settings.expected_tool_inventory_fingerprint
                ),
            )
            # ZER-8 S8: the reporter is given the VERIFYING provider, never left
            # on its ``NoCapabilityEvidenceProvider`` default. That default
            # returns no evidence, and no evidence can be ENFORCED -- so an
            # unwired reporter silently classifies every attested run as
            # ADMISSION while every component test stays green. Binding is safe
            # at process scope because a deployment is tenant-pinned (see the
            # WS-B note above): one tenant, one deployment, for this service's
            # whole lifetime. ``expected_graph_version`` comes from the
            # deployment record rather than the client-submitted registration,
            # so a client cannot satisfy its own version check.
            gateway_capability_reporter = CapabilityReporter(
                PersistedCapabilityEvidenceProvider(
                    attestations=run_attestation_repository,
                    registrations=inventory_registration_repository,
                    signer=signer,
                    tenant_id=deployment.tenant_id,
                    deployment_ref=deployment.deployment_ref,
                    expected_graph_version=deployment.graph_version_ref,
                ),
                governance_evidence_provider=StoredCapabilityEvidenceProvider(
                    langgraph_enforcement_repository,
                    signer,
                    tenant_id=deployment.tenant_id,
                    deployment_ref=str(gateway_settings.deployment_ref),
                ),
                stale_after_seconds=gateway_settings.stale_threshold_seconds,
                expected_graph_version=deployment.graph_version_ref,
            )
            # Constructed here rather than left to the sink's private fallback:
            # that fallback builds its own MetricsCollector, so every delivery
            # counter -- queued, retried, rejected, failed -- would increment
            # onto a registry nothing scrapes, and the delivery stage would be
            # invisible on /v1/metrics. The same instance is what the lifespan
            # drains and what the health surface reports.
            audit_delivery_queue = AuditDeliveryQueue(audit_repository, metrics=metrics_collector)
            event_sink = AuditGatewayEventSink(
                audit_repository,
                actor_for=lambda event: ActorIdentity(
                    subject=event.correlation.principal_id,
                    auth_method=AuthMethod.API_KEY,
                    tenant_id=event.correlation.tenant_id,
                ),
                delivery=audit_delivery_queue,
            )
            gateway_proxy = GatewayProxy(
                settings=gateway_settings,
                transport=gateway_transport,
                context_codec=context_codec,
                policy_guard=policy_guard,
                budget_checker=budget_enforcer,
                compatibility=gateway_compatibility,
                capability_reporter=gateway_capability_reporter,
                event_sink=event_sink,
            )
            gateway_websocket_handler = WebSocketGatewayHandler(
                settings=gateway_settings,
                transport=gateway_transport,
                context_codec=context_codec,
                policy_guard=policy_guard,
                budget_checker=budget_enforcer,
            )
        except BaseException:
            await gateway_transport.aclose()
            raise

    try:
        bootstrap = ServiceBootstrap(
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
            probe_instrumentation=probe_instrumentation,
            memory_registry=memory_registry,
            memory_connector_config_repository=memory_connector_config_repository,
            mcp_server_config_repository=mcp_server_config_repository,
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
            template_dependency_checker=template_dependency_checker,
            subgraph_executor=subgraph_executor,
            secret_provider=secret_provider,
            signer=signer,
            verifier=verifier,
            certification_service=certification_service,
            serving_artifact_identity=serving_artifact_identity,
            policy_guard=policy_guard,
            langgraph_gateway_proxy=gateway_proxy,
            langgraph_gateway_transport=gateway_transport,
            langgraph_gateway_compatibility=gateway_compatibility,
            langgraph_gateway_capability_reporter=gateway_capability_reporter,
            langgraph_enforcement_service=langgraph_enforcement_service,
            langgraph_gateway_websocket_handler=gateway_websocket_handler,
            audit_delivery_queue=audit_delivery_queue,
            retention_policy_repository=retention_policy_repository,
            legal_hold_repository=legal_hold_repository,
            retention_log_repository=retention_log_repository,
            retention_erasure_service=retention_erasure_service,
            retention_worker=retention_worker_obj,
            decision_repository=decision_repository,
            tool_decision_service=tool_decision_service,
            inventory_registration_repository=inventory_registration_repository,
            run_attestation_repository=run_attestation_repository,
            enforcement_heartbeat_repository=enforcement_heartbeat_repository,
            github_repository=github_repository,
            github_client=github_client,
            github_token_broker=github_token_broker,
            github_integration_service=github_integration_service,
            github_maintenance_worker=github_maintenance_worker,
            github_webhook_secret_resolver=github_webhook_secret_resolver,
            repo_checkout_repository=repo_checkout_repository,
            repo_run_repository=repo_run_repository,
            repository_unit_service=repository_unit_service,
            repo_run_worker=repo_run_worker,
            # The *configured* freshness window, so the status routes report
            # the threshold this deployment actually runs with rather than the
            # module default. Read unconditionally: the setting always has a
            # value, and a branch here would add a decision point to a
            # function already at the complexity ceiling.
            enforcement_stale_after_seconds=float(gateway_settings.stale_threshold_seconds),
        )
    except BaseException:
        if gateway_transport is not None:
            await gateway_transport.aclose()
        raise
    bootstrap.guardrail_policy_repository = guardrail_policy_repository
    bootstrap.role_registry = role_registry
    return bootstrap


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
    """Build the reserved-default deployment service with the legacy signature."""
    return await bootstrap_scoped_service(
        database,
        deployment_ref=deployment_ref,
        agent_runners=agent_runners,
        executable_unit_runner=executable_unit_runner,
        auth_config=auth_config,
        bearer_token_verifier=bearer_token_verifier,
        guardrail_config=guardrail_config,
        enable_durable_worker=enable_durable_worker,
        secret_provider=secret_provider,
    )


async def bootstrap_scoped_app(
    database: AsyncDatabase,
    *,
    deployment_ref: str,
    tenant_id: str = "default",
    workspace_id: str | None = None,
    agent_runners: Mapping[str, AgentRunner] | None = None,
    executable_unit_runner: ExecutableUnitRunner | None = None,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
) -> FastAPI:
    """Build the FastAPI app for a specific deployment."""
    return create_app(
        await bootstrap_scoped_service(
            database,
            deployment_ref=deployment_ref,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            agent_runners=agent_runners,
            executable_unit_runner=executable_unit_runner,
            auth_config=auth_config,
            bearer_token_verifier=bearer_token_verifier,
        )
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
    """Build the reserved-default deployment app with the legacy signature."""
    return await bootstrap_scoped_app(
        database,
        deployment_ref=deployment_ref,
        agent_runners=agent_runners,
        executable_unit_runner=executable_unit_runner,
        auth_config=auth_config,
        bearer_token_verifier=bearer_token_verifier,
    )


async def build_runners_for_deployment(
    database: AsyncDatabase,
    deployment_ref: str,
    *,
    tenant_id: str = "default",
    workspace_id: str | None = None,
    provider: ProviderAdapter | None = None,
    secret_provider: SecretProvider | None = None,
    allow_env_fallback: bool = True,
    llm_key_map: dict[str, str] | None = None,
    llm_base_url_map: dict[str, str] | None = None,
) -> dict[str, AgentRunner] | None:
    """Build runners for the graph behind a deployment ref.

    Returns None when the deployment does not exist — bootstrap_service
    raises its own, clearer error for that case.

    ``tenant_id`` for key resolution is taken from ``deployment.tenant_id`` —
    one value per deployment under the single-tenant model.

    This is deployment-fetch wiring, so it lives with service bootstrap: the
    runtime factory builds runners from a graph it is handed, while this
    helper resolves the deployment and constructs the concrete repository.
    """
    deployment = await SQLiteDeploymentRepository(database).get(
        deployment_ref,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    if deployment is None:
        return None
    graph = hydrate_deployed_graph(deployment)
    registry = ContractRegistry.scoped(
        database,
        contract_scope_context(deployment.tenant_id, deployment.workspace_id),
    )
    from zeroth.runtime.agents import RepositoryThreadStateStore

    thread_state_store = RepositoryThreadStateStore(
        database,
        tenant_id=deployment.tenant_id,
        workspace_id=deployment.workspace_id,
    )
    deployment_repository = SQLiteDeploymentRepository(database)
    runners: dict[str, AgentRunner] = {}
    runner_origins: dict[str, tuple[str, int, str]] = {}

    async def visit(
        current_graph: Graph,
        *,
        current_ref: str | None,
        current_deployment_version: int,
        depth: int,
        visited_refs: tuple[str, ...],
    ) -> None:
        local_runners = await build_agent_runners(
            current_graph,
            registry,
            provider=provider,
            secret_provider=secret_provider,
            tenant_id=deployment.tenant_id,
            allow_env_fallback=allow_env_fallback,
            llm_key_map=llm_key_map,
            llm_base_url_map=llm_base_url_map,
            thread_state_store=thread_state_store,
        )
        for authored_id, runner in local_runners.items():
            key = (
                authored_id
                if current_ref is None
                else f"subgraph:{current_ref}:{depth}:{authored_id}"
            )
            origin = (
                current_ref or deployment.deployment_ref,
                current_deployment_version,
                authored_id,
            )
            previous = runner_origins.get(key)
            if previous is not None and previous != origin:
                raise DeploymentBootstrapError(
                    f"agent runner key collision for {key!r}: {previous!r} and {origin!r}"
                )
            runner_origins[key] = origin
            runners.setdefault(key, runner)

        for node in current_graph.nodes:
            if not isinstance(node, SubgraphNode):
                continue
            child_ref = node.subgraph.graph_ref
            child_depth = depth + 1
            if child_depth > node.subgraph.max_depth:
                raise DeploymentBootstrapError(
                    f"subgraph depth {child_depth} exceeds max_depth "
                    f"{node.subgraph.max_depth} for graph_ref {child_ref!r}"
                )
            if child_ref in visited_refs:
                raise DeploymentBootstrapError(
                    f"circular subgraph reference detected while building runners: {child_ref}"
                )
            child_deployment = await deployment_repository.get(
                child_ref,
                node.subgraph.version,
                tenant_id=deployment.tenant_id,
                workspace_id=deployment.workspace_id,
            )
            if child_deployment is None:
                version_part = f" version {node.subgraph.version}" if node.subgraph.version else ""
                raise DeploymentBootstrapError(
                    f"subgraph deployment {child_ref!r}{version_part} not found in served scope"
                )
            try:
                child_graph = hydrate_deployed_graph(child_deployment)
            except Exception as exc:
                raise DeploymentBootstrapError(
                    f"failed to deserialize subgraph deployment {child_ref!r}"
                ) from exc
            await visit(
                child_graph,
                current_ref=child_ref,
                current_deployment_version=child_deployment.version,
                depth=child_depth,
                visited_refs=(*visited_refs, child_ref),
            )

    await visit(
        graph,
        current_ref=None,
        current_deployment_version=deployment.version,
        depth=0,
        visited_refs=(),
    )
    return runners
