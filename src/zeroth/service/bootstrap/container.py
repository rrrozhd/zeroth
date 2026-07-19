"""Dependency container for the deployment-scoped service surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroth.core.approvals import ApprovalService
    from zeroth.core.audit import AuditRepository
    from zeroth.core.contracts import ContractRegistry
    from zeroth.core.deployments import Deployment, DeploymentService
    from zeroth.core.econ.client import RegulusClient
    from zeroth.core.graph import Graph, GraphRepository
    from zeroth.core.guardrails.config import GuardrailConfig
    from zeroth.core.guardrails.dead_letter import DeadLetterManager
    from zeroth.core.guardrails.rate_limit import QuotaEnforcer, TokenBucketRateLimiter
    from zeroth.core.memory.config_repository import MemoryConnectorConfigRepository
    from zeroth.core.memory.registry import InMemoryConnectorRegistry
    from zeroth.core.observability.metrics import MetricsCollector
    from zeroth.core.observability.queue_gauge import QueueDepthGauge
    from zeroth.core.orchestrator import RuntimeOrchestrator
    from zeroth.core.secrets import SecretProvider
    from zeroth.core.signing import SigningKeyProvider
    from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
    from zeroth.platform.dispatch import LeaseManager
    from zeroth.platform.storage import AsyncDatabase
    from zeroth.runtime.orchestration.run_worker import RunWorker
    from zeroth.service.api.authentication import ServiceAuthConfig, ServiceAuthenticator


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
