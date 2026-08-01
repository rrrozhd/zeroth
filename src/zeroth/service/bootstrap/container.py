"""Dependency container for the deployment-scoped service surface."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroth.contracts.graph import Graph, GraphRepository
    from zeroth.contracts.registry import ContractRegistry
    from zeroth.core.langgraph_gateway.models import CompatibilityResult
    from zeroth.core.orchestrator import RuntimeOrchestrator
    from zeroth.econ.analytics.client import RegulusClient
    from zeroth.governance.approvals import ApprovalService
    from zeroth.governance.audit import AuditRepository
    from zeroth.governance.guardrails.config import GuardrailConfig
    from zeroth.governance.guardrails.dead_letter import DeadLetterManager
    from zeroth.governance.guardrails.rate_limit import (
        QuotaEnforcer,
        TokenBucketRateLimiter,
    )
    from zeroth.integrations.memory.config_repository import (
        MemoryConnectorConfigRepository,
    )
    from zeroth.integrations.memory.registry import InMemoryConnectorRegistry
    from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
    from zeroth.platform.dispatch import LeaseManager
    from zeroth.platform.observability.metrics import MetricsCollector
    from zeroth.platform.observability.queue_gauge import QueueDepthGauge
    from zeroth.platform.secrets import SecretProvider
    from zeroth.platform.signing import SigningKeyProvider
    from zeroth.platform.storage import AsyncDatabase
    from zeroth.runtime.orchestration.run_worker import RunWorker
    from zeroth.service.api.authentication import (
        ServiceAuthConfig,
        ServiceAuthenticator,
    )
    from zeroth.service.deployments import Deployment, DeploymentService


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
    role_registry: object = field(default_factory=lambda: _default_role_registry(), init=False)
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
    # WS-D verify side. Holds the active key AND every key rotated away from, so
    # a row signed under a retired key stays verifiable after rotation. Present
    # even when ``signer`` is absent (signing disabled), because rows signed
    # before signing was turned off still have to verify. Never used to sign.
    verifier: SigningKeyProvider | None = None
    # LangGraph Agent Server gateway foundation. All remain absent when the
    # mode is disabled so the ordinary service creates no upstream client or
    # probe traffic.
    policy_guard: object | None = None
    langgraph_gateway_proxy: object | None = None
    langgraph_gateway_transport: object | None = None
    langgraph_gateway_compatibility: CompatibilityResult | None = None
    langgraph_gateway_capability_reporter: object | None = None
    langgraph_gateway_websocket_handler: object | None = None
    # The bounded audit-delivery stage the gateway event sink submits into.
    # Held here because two owners outside the sink need it: the lifespan,
    # which drains it after the transport stops and before the database goes
    # away, and the health surface, which reports its depth and its failures.
    # A sink that kept it private would make both of those unreachable.
    audit_delivery_queue: object | None = None
    # WS-E: retention / right-to-erasure surface (per-tenant TTLs, legal holds,
    # full-surface erasure that preserves the audit hash-chain). Always wired;
    # the purge WORKER is only started when ZEROTH_RETENTION__ENABLED is true.
    retention_policy_repository: object | None = None
    legal_hold_repository: object | None = None
    retention_log_repository: object | None = None
    retention_erasure_service: object | None = None
    retention_worker: object | None = None
    # ZER-8: tool-enforcement surface. The decision service and the three
    # evidence stores the SDK adapter writes to over HTTP. Always wired against
    # the same database; optional here only so an application composed by hand
    # (and the route-inventory snapshots, which build an app from a bare
    # namespace) still constructs.
    decision_repository: object | None = None
    tool_decision_service: object | None = None
    inventory_registration_repository: object | None = None
    run_attestation_repository: object | None = None
    enforcement_heartbeat_repository: object | None = None
    enforcement_stale_after_seconds: float | None = None
    """Configured heartbeat freshness window, from
    ``LangGraphGatewaySettings.stale_threshold_seconds``.

    Carried on the container because the enforcement status routes must
    report the threshold this deployment is *configured* with, not the
    module default. ``None`` leaves the routes on that default.
    """


_bootstrap_parameters = inspect.signature(ServiceBootstrap).parameters
ServiceBootstrap.__signature__ = inspect.signature(ServiceBootstrap).replace(
    parameters=[
        parameter
        for name, parameter in _bootstrap_parameters.items()
        if name
        not in {
            "policy_guard",
            "langgraph_gateway_proxy",
            "langgraph_gateway_transport",
            "langgraph_gateway_compatibility",
            "langgraph_gateway_capability_reporter",
            "langgraph_gateway_websocket_handler",
            "audit_delivery_queue",
            # ZER-8 fields are hidden from the introspected signature for the
            # same reason the gateway's are: the protected surface fixture pins
            # ``ServiceBootstrap.__init__``, and an additive keyword-only
            # component is not a change to the capability that fixture names.
            "decision_repository",
            "tool_decision_service",
            "inventory_registration_repository",
            "run_attestation_repository",
            "enforcement_heartbeat_repository",
            "enforcement_stale_after_seconds",
            # Same reason again: the verify-side provider is an additive
            # component, not a change to the capability the fixture names.
            "verifier",
        }
    ]
)


def _default_role_registry() -> object:
    from zeroth.service.api.authorization import RoleRegistry

    return RoleRegistry()
