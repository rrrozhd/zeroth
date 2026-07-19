# Backend Import Migration Guide

This guide is the change log for public Python import locations during the
backend architecture refactor. The baseline captured on 2026-07-18 has not
moved any symbols: all public imports still resolve from their legacy
locations. The canonical package shells exist so moves can proceed in focused,
independently verified slices.

## Compatibility policy

- A useful library capability may move to a clearer domain package, but its
  call signature, return behavior, and public exception semantics remain
  stable.
- A temporary re-export is optional. Consumers should migrate to the canonical
  import recorded here instead of relying on compatibility shims.
- `tests/contracts/fixtures/backend_surface_legacy.json` is immutable after
  the corrected baseline inventory is accepted. It identifies protected
  capabilities independently of their future import locations.
- `tests/contracts/fixtures/backend_surface_canonical.json` is evolving. Every
  edit to it must be committed separately from production moves and accompanied
  by a row in this guide.
- Superseded or removed symbols require explicit dead-code evidence covering
  static reachability, dynamic registration, exports, documentation, examples,
  service schemas, and optional integrations.

## Initial package dispositions

These rows establish the approved package-level destinations. A **move** row
does not claim that its public symbols have moved; the status remains
`Skeleton only` until a production slice and its separate canonical-surface
update are committed. **Unchanged** rows stay at their current paths unless a
separate design amendment approves a move.

| Current package | Canonical package | Disposition | Initial status |
| --- | --- | --- | --- |
| `zeroth.core.orchestrator`, `zeroth.core.agent_runtime`, `zeroth.core.parallel`, `zeroth.core.subgraph`, `zeroth.core.context_window` | `zeroth.runtime.orchestration`, `zeroth.runtime.agents`, `zeroth.runtime.parallel`, `zeroth.runtime.subgraphs`, `zeroth.runtime.context` | Move and decompose | Skeleton only |
| `zeroth.core.approvals`, `zeroth.core.audit`, `zeroth.core.identity`, `zeroth.core.policy`, `zeroth.core.guardrails`, `zeroth.core.retention` | `zeroth.governance.approvals`, `zeroth.governance.audit`, `zeroth.governance.identity`, `zeroth.governance.policy`, `zeroth.governance.guardrails`, `zeroth.governance.retention` | Move; decompose retention | Skeleton only |
| `zeroth.core.artifacts`, `zeroth.core.config`, `zeroth.core.dispatch`, `zeroth.core.observability`, `zeroth.core.secrets`, `zeroth.core.signing`, `zeroth.core.storage` | `zeroth.platform.artifacts`, `zeroth.platform.config`, `zeroth.platform.dispatch`, `zeroth.platform.observability`, `zeroth.platform.secrets`, `zeroth.platform.signing`, `zeroth.platform.storage` | Move; add shared persistence and primitives | Canonical import path published (Task 11; the governed store factory landed in `zeroth.integrations.persistence.governed_redis` and the run worker in `zeroth.runtime.orchestration.run_worker`) |
| `zeroth.core.conditions`, `zeroth.core.contracts`, `zeroth.core.graph`, `zeroth.core.mappings`, `zeroth.core.templates` | `zeroth.contracts.conditions`, `zeroth.contracts.registry`, `zeroth.contracts.graph`, `zeroth.contracts.mappings`, `zeroth.contracts.templates` | Move; decompose graph validation | Skeleton only |
| `zeroth.core.runs` models and protocols | `zeroth.runtime.runs` | Move domain contracts | Canonical import path published |
| `zeroth.core.runs` SQL persistence | `zeroth.integrations.persistence.runs` | Move and decompose persistence adapters | Canonical import path published |
| `zeroth.core.service`, `zeroth.core.deployments`, `zeroth.core.webhooks` | `zeroth.service.api`, `zeroth.service.bootstrap`, `zeroth.service.deployments`, `zeroth.service.webhooks` | Move; decompose bootstrap | Skeleton only |
| `zeroth.core.econ`, `zeroth.econ_plane` | `zeroth.econ.analytics`, `zeroth.econ.instrumentation`, `zeroth.econ.plane` | Move and consolidate | Skeleton only |
| `zeroth.core.execution_units`, `zeroth.core.http`, `zeroth.core.memory`, `zeroth.core.rag`, `zeroth.core.sandbox_sidecar` | `zeroth.integrations.execution`, `zeroth.integrations.http`, `zeroth.integrations.memory`, `zeroth.integrations.rag`, `zeroth.integrations.sandbox` | Move; preserve optional integrations | Skeleton only |
| `zeroth.core.eval` | `zeroth.eval` | Move stable evaluation capability | Skeleton only |
| `zeroth.core.governed.app`, `zeroth.core.governed.models` | `zeroth.contracts.governed` | Move and consolidate specifications | Skeleton only |
| `zeroth.core.governed.runtime`, `zeroth.core.governed.tools` | `zeroth.runtime.orchestration`, `zeroth.runtime.agents` | Move into maintained runtime boundaries | Skeleton only |
| `zeroth.core.governed.audit`, `zeroth.core.governed.memory`, `zeroth.core.governed.integrations` | `zeroth.governance.audit`, `zeroth.integrations.memory`, relevant integration packages | Move only after capability inventory | Skeleton only |
| `zeroth.core.demos`, `zeroth.core.examples`, `zeroth.core.migrations`, `zeroth.econ_plane._migrations` | Existing locations | Unchanged | Unchanged by this refactor |
| `zeroth.core` package shell and top-level CLI/entry points | Existing locations during migration | Unchanged compatibility shell | Unchanged during staged moves |

## Symbol migration log

Entries use this exact schema and are added only with the separate
canonical-surface update that follows a verified production move.

| Old path and symbol | New path and symbol | Disposition | Compatibility status | Replacement | Removal evidence |
| --- | --- | --- | --- | --- | --- |
| `zeroth.core.runs:Run` | `zeroth.runtime.runs:Run` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunConditionResult` | `zeroth.runtime.runs:RunConditionResult` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunFailureState` | `zeroth.runtime.runs:RunFailureState` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunHistoryEntry` | `zeroth.runtime.runs:RunHistoryEntry` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunState` | `zeroth.runtime.runs:RunState` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunStatus` | `zeroth.runtime.runs:RunStatus` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:Thread` | `zeroth.runtime.runs:Thread` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:ThreadMemoryBinding` | `zeroth.runtime.runs:ThreadMemoryBinding` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:ThreadStatus` | `zeroth.runtime.runs:ThreadStatus` | Move to runtime run domain | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:RunRepository` | `zeroth.integrations.persistence.runs:RunRepository` | Move to concrete persistence | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs:ThreadRepository` | `zeroth.integrations.persistence.runs:ThreadRepository` | Move to concrete persistence | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.graph.validation:GraphValidator` | `zeroth.runtime.graph_validation:GraphValidator` | Move composed validator to runtime | Legacy path still re-exports, lazily | Same class object | Not removed |
| `zeroth.core.execution_units.inline:INLINE_SOURCE_MAX_CHARS` | `zeroth.contracts.graph.limits:INLINE_SOURCE_MAX_CHARS` | Move authoring limit to contracts | Legacy path still re-exports | Same object | Not removed |
| `zeroth.core.orchestrator.runtime:OrchestratorError` | `zeroth.runtime.orchestration.errors:OrchestratorError` | Move runtime exceptions to the canonical package | Legacy paths still re-export | Same class object | Not removed |
| `zeroth.core.orchestrator.runtime:NodeDispatcherError` | `zeroth.runtime.orchestration.errors:NodeDispatcherError` | Move runtime exceptions to the canonical package | Legacy paths still re-export | Same class object | Not removed |
| `zeroth.core.orchestrator.runtime:MemoryBindingResolutionError` | `zeroth.runtime.orchestration.errors:MemoryBindingResolutionError` | Move runtime exceptions to the canonical package | Legacy paths still re-export | Same class object | Not removed |
| `zeroth.core.retention:SqlAlchemyEconEventEraser` | `zeroth.econ.plane.erasure:SqlAlchemyEconEventEraser` | Move concrete econ adapter to the econ domain | Legacy paths still re-export, lazily | Same class object | Not removed |
| `zeroth.core.retention.erasure_service:LegalHoldError` | `zeroth.governance.retention.errors:LegalHoldError` | Move retention exceptions to the canonical package | Legacy paths still re-export | Same class object | Not removed |
| `zeroth.core.retention.erasure_service:StaleCleanupClaimError` | `zeroth.governance.retention.errors:StaleCleanupClaimError` | Move retention exceptions to the canonical package | Legacy paths still re-export | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:CreateContractRequest` | `zeroth.service.api.studio_schemas:CreateContractRequest` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:CreateWorkflowRequest` | `zeroth.service.api.studio_schemas:CreateWorkflowRequest` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:NodeTypeResponse` | `zeroth.service.api.studio_schemas:NodeTypeResponse` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:PortDefinitionResponse` | `zeroth.service.api.studio_schemas:PortDefinitionResponse` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:StudioContractResponse` | `zeroth.service.api.studio_schemas:StudioContractResponse` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:StudioEdgeResponse` | `zeroth.service.api.studio_schemas:StudioEdgeResponse` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:StudioNodeResponse` | `zeroth.service.api.studio_schemas:StudioNodeResponse` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:StudioPosition` | `zeroth.service.api.studio_schemas:StudioPosition` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:StudioViewport` | `zeroth.service.api.studio_schemas:StudioViewport` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:UpdateWorkflowRequest` | `zeroth.service.api.studio_schemas:UpdateWorkflowRequest` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:WorkflowDetailResponse` | `zeroth.service.api.studio_schemas:WorkflowDetailResponse` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.studio_schemas:WorkflowSummaryResponse` | `zeroth.service.api.studio_schemas:WorkflowSummaryResponse` | Move Studio schema models to the service API package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.bootstrap:run_migrations` | `zeroth.service.bootstrap.migrations:run_migrations` | Decompose service bootstrap | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.service.bootstrap:ServiceBootstrap` | `zeroth.service.bootstrap.container:ServiceBootstrap` | Decompose service bootstrap | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.bootstrap:DeploymentBootstrapError` | `zeroth.service.bootstrap.container:DeploymentBootstrapError` | Decompose service bootstrap | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.bootstrap:bootstrap_service` | `zeroth.service.bootstrap.factory:bootstrap_service` | Decompose service bootstrap | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.service.bootstrap:bootstrap_app` | `zeroth.service.bootstrap.factory:bootstrap_app` | Decompose service bootstrap | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.service.admin_api:AdminRunListResponse` | `zeroth.service.api.admin_api:AdminRunListResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.auth:AuthenticationError` | `zeroth.service.api.authentication:AuthenticationError` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.auth:BearerTokenConfig` | `zeroth.service.api.authentication:BearerTokenConfig` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.auth:ServiceAuthConfig` | `zeroth.service.api.authentication:ServiceAuthConfig` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.auth:StaticApiKeyCredential` | `zeroth.service.api.authentication:StaticApiKeyCredential` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.contracts_api:DeploymentResultErrorStateSchemaResponse` | `zeroth.service.api.contracts_api:DeploymentResultErrorStateSchemaResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.contracts_api:DeploymentVersionMetadataResponse` | `zeroth.service.api.contracts_api:DeploymentVersionMetadataResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.contracts_api:PublicContractSchemaResponse` | `zeroth.service.api.contracts_api:PublicContractSchemaResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.econ_analytics_api:QualityVerdictRequest` | `zeroth.service.api.econ_analytics_api:QualityVerdictRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.manifest_api:ManifestSummaryResponse` | `zeroth.service.api.manifest_api:ManifestSummaryResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.run_api:ApprovalPausedState` | `zeroth.service.api.run_api:ApprovalPausedState` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.run_api:RunInvocationRequest` | `zeroth.service.api.run_api:RunInvocationRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.run_api:RunInvocationResponse` | `zeroth.service.api.run_api:RunInvocationResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.run_api:RunStatusResponse` | `zeroth.service.api.run_api:RunStatusResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.approval_api:ApprovalResolutionRequest` | `zeroth.service.api.approval_api:ApprovalResolutionRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.approval_api:ApprovalResolutionResponse` | `zeroth.service.api.approval_api:ApprovalResolutionResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.connector_api:ConnectorCreateRequest` | `zeroth.service.api.connector_api:ConnectorCreateRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.connector_api:ConnectorSummaryResponse` | `zeroth.service.api.connector_api:ConnectorSummaryResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.connector_api:ConnectorTestResponse` | `zeroth.service.api.connector_api:ConnectorTestResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.connector_api:ConnectorUpdateRequest` | `zeroth.service.api.connector_api:ConnectorUpdateRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.cost_api:DeploymentCostResponse` | `zeroth.service.api.cost_api:DeploymentCostResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.cost_api:TenantBudgetRequest` | `zeroth.service.api.cost_api:TenantBudgetRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.cost_api:TenantCostResponse` | `zeroth.service.api.cost_api:TenantCostResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.deployment_api:CreateDeploymentRequest` | `zeroth.service.api.deployment_api:CreateDeploymentRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.deployment_api:DeploymentSummaryResponse` | `zeroth.service.api.deployment_api:DeploymentSummaryResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.deployment_api:RollbackDeploymentRequest` | `zeroth.service.api.deployment_api:RollbackDeploymentRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.rightsizing_api:ExperimentRequest` | `zeroth.service.api.rightsizing_api:ExperimentRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.rightsizing_api:RightsizingRequest` | `zeroth.service.api.rightsizing_api:RightsizingRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.template_api:CreateTemplateRequest` | `zeroth.service.api.template_api:CreateTemplateRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.template_api:TemplateListResponse` | `zeroth.service.api.template_api:TemplateListResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.template_api:TemplateResponse` | `zeroth.service.api.template_api:TemplateResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.app:HealthResponse` | `zeroth.service.api.health:HealthResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.app:create_app` | `zeroth.service.app:create_app` | Move service API to the canonical package | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.service.audit_api:AttestationVerificationResponse` | `zeroth.service.api.audit_api:AttestationVerificationResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.audit_api:AuditRecordListResponse` | `zeroth.service.api.audit_api:AuditRecordListResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.audit_api:AuditTimelineResponse` | `zeroth.service.api.audit_api:AuditTimelineResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.audit_api:AuditVerificationResponse` | `zeroth.service.api.audit_api:AuditVerificationResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.audit_api:DeploymentAttestationResponse` | `zeroth.service.api.audit_api:DeploymentAttestationResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.audit_api:DeploymentEvidenceResponse` | `zeroth.service.api.audit_api:DeploymentEvidenceResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.audit_api:EvidenceSummaryResponse` | `zeroth.service.api.audit_api:EvidenceSummaryResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.audit_api:RunEvidenceResponse` | `zeroth.service.api.audit_api:RunEvidenceResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.audit_api:VerifyChainRequest` | `zeroth.service.api.audit_api:VerifyChainRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.health:DependencyStatus` | `zeroth.service.api.health:DependencyStatus` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.health:LivenessResponse` | `zeroth.service.api.health:LivenessResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.health:ReadinessResponse` | `zeroth.service.api.health:ReadinessResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.retention_api:ErasureRequestBody` | `zeroth.service.api.retention_api:ErasureRequestBody` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.retention_api:ErasureResponse` | `zeroth.service.api.retention_api:ErasureResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.retention_api:ErasureRunResult` | `zeroth.service.api.retention_api:ErasureRunResult` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.retention_api:LegalHoldBody` | `zeroth.service.api.retention_api:LegalHoldBody` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.retention_api:LegalHoldResponse` | `zeroth.service.api.retention_api:LegalHoldResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.retention_api:RetentionPolicyBody` | `zeroth.service.api.retention_api:RetentionPolicyBody` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.retention_api:RetentionPolicyResponse` | `zeroth.service.api.retention_api:RetentionPolicyResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.webhook_api:CreateSubscriptionRequest` | `zeroth.service.api.webhook_api:CreateSubscriptionRequest` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.webhook_api:WebhookDeadLetterListResponse` | `zeroth.service.api.webhook_api:WebhookDeadLetterListResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.webhook_api:WebhookDeadLetterResponse` | `zeroth.service.api.webhook_api:WebhookDeadLetterResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.webhook_api:WebhookSubscriptionListResponse` | `zeroth.service.api.webhook_api:WebhookSubscriptionListResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.service.webhook_api:WebhookSubscriptionResponse` | `zeroth.service.api.webhook_api:WebhookSubscriptionResponse` | Move service API to the canonical package | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.config:ZerothSettings` | `zeroth.platform.config:ZerothSettings` | Move config to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.config:get_settings` | `zeroth.platform.config:get_settings` | Move config to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.config.settings:get_settings` | `zeroth.platform.config.settings:get_settings` | Move config to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.econ.models:RegulusSettings` | `zeroth.platform.config.models:RegulusSettings` | Move composed settings section into platform config | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.http.models:HttpClientSettings` | `zeroth.platform.config.models:HttpClientSettings` | Move composed settings section into platform config | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage:AsyncConnection` | `zeroth.platform.storage:AsyncConnection` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage:AsyncDatabase` | `zeroth.platform.storage:AsyncDatabase` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage:AsyncPostgresDatabase` | `zeroth.platform.storage:AsyncPostgresDatabase` | Move storage to platform | Legacy path still re-exports, lazily | Same class object | Not removed |
| `zeroth.core.storage:AsyncSQLiteDatabase` | `zeroth.platform.storage:AsyncSQLiteDatabase` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage:EncryptedField` | `zeroth.platform.storage:EncryptedField` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage:Migration` | `zeroth.platform.storage:Migration` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage:RedisConfig` | `zeroth.platform.storage:RedisConfig` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage:RedisDeploymentMode` | `zeroth.platform.storage:RedisDeploymentMode` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage:SQLiteDatabase` | `zeroth.platform.storage:SQLiteDatabase` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage:create_database` | `zeroth.platform.storage:create_database` | Move storage to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.storage:docker_container_running` | `zeroth.platform.storage:docker_container_running` | Move storage to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.storage:ensure_and_lock_row` | `zeroth.platform.storage:ensure_and_lock_row` | Move storage to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.storage:GovernAIRedisRuntimeStores` | `zeroth.integrations.persistence.governed_redis:GovernAIRedisRuntimeStores` | Move governed store factory to integrations | Legacy paths still re-export, lazily | Same class object | Not removed |
| `zeroth.core.storage:build_governai_redis_runtime` | `zeroth.integrations.persistence.governed_redis:build_governai_redis_runtime` | Move governed store factory to integrations | Legacy paths still re-export, lazily | Same function object | Not removed |
| `zeroth.core.storage.async_sqlite:AsyncSQLiteDatabase` | `zeroth.platform.storage.async_sqlite:AsyncSQLiteDatabase` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage.database:CoordinationTimeoutError` | `zeroth.platform.storage.database:CoordinationTimeoutError` | Move storage to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.storage.factory:create_database` | `zeroth.platform.storage.factory:create_database` | Move storage to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.artifacts:ArtifactNotFoundError` | `zeroth.platform.artifacts:ArtifactNotFoundError` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts:ArtifactReference` | `zeroth.platform.artifacts:ArtifactReference` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts:ArtifactStorageError` | `zeroth.platform.artifacts:ArtifactStorageError` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts:ArtifactStore` | `zeroth.platform.artifacts:ArtifactStore` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts:ArtifactStoreError` | `zeroth.platform.artifacts:ArtifactStoreError` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts:ArtifactStoreSettings` | `zeroth.platform.artifacts:ArtifactStoreSettings` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts:ArtifactTTLError` | `zeroth.platform.artifacts:ArtifactTTLError` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts:FilesystemArtifactStore` | `zeroth.platform.artifacts:FilesystemArtifactStore` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts:RedisArtifactStore` | `zeroth.platform.artifacts:RedisArtifactStore` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts:generate_artifact_key` | `zeroth.platform.artifacts:generate_artifact_key` | Move artifacts to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.artifacts.errors:ArtifactNotFoundError` | `zeroth.platform.artifacts.errors:ArtifactNotFoundError` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts.errors:ArtifactStorageError` | `zeroth.platform.artifacts.errors:ArtifactStorageError` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts.errors:ArtifactStoreError` | `zeroth.platform.artifacts.errors:ArtifactStoreError` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts.errors:ArtifactTTLError` | `zeroth.platform.artifacts.errors:ArtifactTTLError` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts.models:ArtifactReference` | `zeroth.platform.artifacts.models:ArtifactReference` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.artifacts.models:ArtifactStoreSettings` | `zeroth.platform.artifacts.models:ArtifactStoreSettings` | Move artifacts to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.dispatch:LeaseManager` | `zeroth.platform.dispatch:LeaseManager` | Move dispatch to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.dispatch:RunWorker` | `zeroth.runtime.orchestration.run_worker:RunWorker` | Move run worker to the runtime layer | Legacy path still re-exports, lazily | Same class object | Not removed |
| `zeroth.core.dispatch:WAKEUP_TASK_NAME` | `zeroth.platform.dispatch:WAKEUP_TASK_NAME` | Move dispatch to platform | Legacy path still re-exports | Same object | Not removed |
| `zeroth.core.dispatch:arq_settings_from_zeroth` | `zeroth.platform.dispatch:arq_settings_from_zeroth` | Move dispatch to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.dispatch:create_arq_pool` | `zeroth.platform.dispatch:create_arq_pool` | Move dispatch to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.dispatch:enqueue_wakeup` | `zeroth.platform.dispatch:enqueue_wakeup` | Move dispatch to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.dispatch:run_arq_consumer` | `zeroth.platform.dispatch:run_arq_consumer` | Move dispatch to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.dispatch.arq_wakeup:create_arq_pool` | `zeroth.platform.dispatch.arq_wakeup:create_arq_pool` | Move dispatch to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.dispatch.arq_wakeup:enqueue_wakeup` | `zeroth.platform.dispatch.arq_wakeup:enqueue_wakeup` | Move dispatch to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.observability:MetricsCollector` | `zeroth.platform.observability:MetricsCollector` | Move observability to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.observability:configure_tracing` | `zeroth.platform.observability:configure_tracing` | Move observability to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.observability:get_correlation_id` | `zeroth.platform.observability:get_correlation_id` | Move observability to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.observability:new_correlation_id` | `zeroth.platform.observability:new_correlation_id` | Move observability to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.observability:set_correlation_id` | `zeroth.platform.observability:set_correlation_id` | Move observability to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.observability:start_span` | `zeroth.platform.observability:start_span` | Move observability to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets:EnvSecretProvider` | `zeroth.platform.secrets:EnvSecretProvider` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets:SecretProvider` | `zeroth.platform.secrets:SecretProvider` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets:SecretProviderConfigError` | `zeroth.platform.secrets:SecretProviderConfigError` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets:SecretRedactor` | `zeroth.platform.secrets:SecretRedactor` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets:SecretResolutionError` | `zeroth.platform.secrets:SecretResolutionError` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets:SecretResolver` | `zeroth.platform.secrets:SecretResolver` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets:VaultSecretProvider` | `zeroth.platform.secrets:VaultSecretProvider` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets:build_secret_provider` | `zeroth.platform.secrets:build_secret_provider` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets:normalize_secret_name` | `zeroth.platform.secrets:normalize_secret_name` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets:resolve_async` | `zeroth.platform.secrets:resolve_async` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets:resolve_many_async` | `zeroth.platform.secrets:resolve_many_async` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets:resolve_secret_async` | `zeroth.platform.secrets:resolve_secret_async` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets.factory:SecretProviderConfigError` | `zeroth.platform.secrets.factory:SecretProviderConfigError` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets.factory:build_secret_provider` | `zeroth.platform.secrets.factory:build_secret_provider` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets.provider:EnvSecretProvider` | `zeroth.platform.secrets.provider:EnvSecretProvider` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets.provider:SecretProvider` | `zeroth.platform.secrets.provider:SecretProvider` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets.provider:SecretResolutionError` | `zeroth.platform.secrets.provider:SecretResolutionError` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets.provider:SecretResolver` | `zeroth.platform.secrets.provider:SecretResolver` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets.provider:normalize_secret_name` | `zeroth.platform.secrets.provider:normalize_secret_name` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets.provider:resolve_async` | `zeroth.platform.secrets.provider:resolve_async` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets.provider:resolve_many_async` | `zeroth.platform.secrets.provider:resolve_many_async` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets.provider:resolve_secret_async` | `zeroth.platform.secrets.provider:resolve_secret_async` | Move secrets to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.secrets.redaction:SecretRedactor` | `zeroth.platform.secrets.redaction:SecretRedactor` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.secrets.vault:VaultSecretProvider` | `zeroth.platform.secrets.vault:VaultSecretProvider` | Move secrets to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.signing:Ed25519Signer` | `zeroth.platform.signing:Ed25519Signer` | Move signing to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.signing:EnvHmacSigner` | `zeroth.platform.signing:EnvHmacSigner` | Move signing to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.signing:NullSigner` | `zeroth.platform.signing:NullSigner` | Move signing to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.signing:SigningConfigError` | `zeroth.platform.signing:SigningConfigError` | Move signing to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.signing:SigningKeyProvider` | `zeroth.platform.signing:SigningKeyProvider` | Move signing to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.signing:build_signing_provider` | `zeroth.platform.signing:build_signing_provider` | Move signing to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.signing:build_signing_provider_async` | `zeroth.platform.signing:build_signing_provider_async` | Move signing to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.signing:sign_digest` | `zeroth.platform.signing:sign_digest` | Move signing to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.signing:signable_bytes` | `zeroth.platform.signing:signable_bytes` | Move signing to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.signing:verify_digest` | `zeroth.platform.signing:verify_digest` | Move signing to platform | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.signing.provider:SigningConfigError` | `zeroth.platform.signing.provider:SigningConfigError` | Move signing to platform | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts:ContractNotFoundError` | `zeroth.contracts.registry:ContractNotFoundError` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts:ContractReference` | `zeroth.contracts.registry:ContractReference` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts:ContractRegistry` | `zeroth.contracts.registry:ContractRegistry` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts:ContractRegistryError` | `zeroth.contracts.registry:ContractRegistryError` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts:ContractVersion` | `zeroth.contracts.registry:ContractVersion` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts:StepContractBinding` | `zeroth.contracts.registry:StepContractBinding` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts:ToolContractBinding` | `zeroth.contracts.registry:ToolContractBinding` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts:validate_artifact_reference` | `zeroth.contracts.registry:validate_artifact_reference` | Move registry contracts | Legacy path still re-exports | Same function object | Not removed |
| `zeroth.core.contracts.errors:ContractNotFoundError` | `zeroth.contracts.registry.errors:ContractNotFoundError` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts.errors:ContractRegistryError` | `zeroth.contracts.registry.errors:ContractRegistryError` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts.errors:ContractTypeResolutionError` | `zeroth.contracts.registry.errors:ContractTypeResolutionError` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.contracts.errors:ContractVersionExistsError` | `zeroth.contracts.registry.errors:ContractVersionExistsError` | Move registry contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:BranchResolution` | `zeroth.contracts.conditions:BranchResolution` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:BranchResolver` | `zeroth.contracts.conditions:BranchResolver` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:ConditionBinder` | `zeroth.contracts.conditions:ConditionBinder` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:ConditionBinding` | `zeroth.contracts.conditions:ConditionBinding` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:ConditionContext` | `zeroth.contracts.conditions:ConditionContext` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:ConditionEvaluator` | `zeroth.contracts.conditions:ConditionEvaluator` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:ConditionOutcome` | `zeroth.contracts.conditions:ConditionOutcome` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:NextStepPlan` | `zeroth.contracts.conditions:NextStepPlan` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:NextStepPlanner` | `zeroth.contracts.conditions:NextStepPlanner` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:TraversalState` | `zeroth.contracts.conditions:TraversalState` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions:ConditionResultRecorder` | `zeroth.runtime.runs:ConditionResultRecorder` | Move condition recording to runtime run domain | Legacy path lazily re-exports | Same class object | Not removed |
| `zeroth.core.conditions.errors:BranchResolutionError` | `zeroth.contracts.conditions.errors:BranchResolutionError` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions.errors:ConditionEvaluationError` | `zeroth.contracts.conditions.errors:ConditionEvaluationError` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions.models:BranchResolution` | `zeroth.contracts.conditions.models:BranchResolution` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions.models:ConditionBinding` | `zeroth.contracts.conditions.models:ConditionBinding` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions.models:ConditionContext` | `zeroth.contracts.conditions.models:ConditionContext` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions.models:ConditionOutcome` | `zeroth.contracts.conditions.models:ConditionOutcome` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions.models:NextStepPlan` | `zeroth.contracts.conditions.models:NextStepPlan` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.conditions.models:TraversalState` | `zeroth.contracts.conditions.models:TraversalState` | Move condition contracts | Legacy path still re-exports | Same class object | Not removed |
| `zeroth.core.runs.models:RunConditionResult` | `zeroth.contracts.conditions.models:RunConditionResult` | Move condition-result definition to conditions contracts | Legacy path still re-exports | Same class object | Not removed |

The two repositories are persistence, not runtime contracts, which is why they
land under `zeroth.integrations.persistence.runs` rather than
`zeroth.runtime.runs`. Runtime code depends on the `RunReader`, `RunWriter`,
`CheckpointStore`, and `ThreadStore` protocols published alongside the models,
and receives a concrete adapter through injection.

### Run serialization and checkpoint storage

The first half of that persistence move is done. Row-to-model conversion now
lives in `zeroth.integrations.persistence.runs.serialization` and the
`run_checkpoints` table adapter in
`zeroth.integrations.persistence.runs.checkpoint_store`.

Neither module appears in the symbol migration log or the canonical surface,
and that is not an omission. Every symbol involved was a private helper —
`_row_to_run`, `_row_to_thread`, `_dump_model`, `_dump_list`,
`_new_checkpoint_id`, and the two `_*_state_json` methods — so none of them
carries a protected legacy capability ID. The log records public import
locations that consumers may depend on; it does not track internal structure.

The split follows the transaction boundary rather than the table names.
`checkpoint_store` owns the `run_checkpoints` rows and the at-rest encryption
of `state_json`; checkpoint *ordering* and the thread bookkeeping around a
write stay with the caller, because both read and write the thread record. In
the previous implementation each of those steps already opened its own
transaction, so delegating only the row write keeps the lock scope identical.
Moving the thread bookkeeping into the checkpoint adapter instead would have
merged transactions that were previously separate.

### Two dependency exceptions this move could not remove

`zeroth.core.runs.repository` is now a pure re-export and nothing inside the
tree depends on it. Two runtime-to-integrations edges survive anyway, and both
are retargeted at the canonical package rather than deleted.

**`zeroth.core.runs` → `zeroth.integrations.persistence.runs`.**
`zeroth.core.runs:RunRepository` and `zeroth.core.runs:ThreadRepository` are
protected legacy capabilities, so the legacy package has to keep republishing
adapters it no longer owns. No package move dissolves this; it ends when the
`zeroth.core` compatibility shell is retired. The resolution stays lazy — an
eager import here reintroduces the cycle that blocked the extraction in the
first place.

**`zeroth.core.agent_runtime.thread_store` →
`zeroth.integrations.persistence.runs`.** `RepositoryThreadStateStore`
constructs `RunRepository` and `ThreadRepository` when it is not handed them,
so this is a real dependency rather than a type-only import. Narrowing it to
the `RunReader`/`ThreadStore` protocols is not available as a local fix: the
constructor signature, including those two type names, is pinned in the
immutable `backend_surface_legacy.json`, and `from __future__ import
annotations` means the pinned string is the annotation source text. Changing
the names fails the legacy-surface gate. The edge therefore moves with the
rest of the agent runtime in Task 14, whose completion check — that runtime
has no import of `zeroth.integrations` — is what will force the constructor
question to be answered properly.

### Why these models are republished rather than relocated

The class definitions still live in `zeroth.core.runs.models`;
`zeroth.runtime.runs` re-exports the same class objects. This is a deliberate
constraint of the protected surface, not an oversight.

`inspect.signature` renders an annotation using the *defining* module of each
referenced type, so relocating these definitions rewrites signature strings
such as `list[zeroth.core.runs.models.RunHistoryEntry]`. Those exact strings are
pinned in the immutable `backend_surface_legacy.json`, and
`test_immutable_legacy_capabilities_remain_available_with_original_signatures`
compares the canonical entry against the legacy one. Relocation therefore fails
that gate whether or not the canonical fixture is updated, and the legacy
fixture may not be edited.

**Resolved 2026-07-18** in `test: compare capability signatures independently
of import location`. `_comparable()` in
`tests/architecture/test_library_surface.py` now normalizes
`zeroth.<anything>.SomeType` to `SomeType` on both sides of every signature
comparison. The fixtures are never rewritten, so the immutability rule holds
literally.

This was forced rather than chosen: 141 of the 895 protected capabilities carry
a `zeroth.*` path in their pinned signature, concentrated in the packages Tasks
10–16 must move — `execution_units` (96), `graph` (51), `service` (26),
`identity` (21), `agent_runtime` (19), `runs` and `config` (17 each),
`approvals` (16), `audit` (15). Raw string comparison made every one of those
moves fail a fixture that cannot be edited.

Physical relocation is therefore available from Task 10 onward. The re-export
approach used in Tasks 5–9 remains valid and does not need unwinding; it is
simply no longer the only option. Accepted cost: two same-named classes in
different packages no longer compare as different — parameter names, order,
defaults and bare type names stay pinned, and
`tests/architecture/test_backend_dependencies.py` independently constrains
which package may supply a symbol.

### Import-direction constraint while the models are republished

**No module reachable from `zeroth.core.__init__` may import
`zeroth.runtime.runs`.**

`zeroth.runtime.runs` re-exports models defined under `zeroth.core`, and
importing anything from `zeroth.core` executes its eager package `__init__`,
which pulls in most of the core graph. A module on that path that imports back
into `zeroth.runtime.runs` closes a cycle, and the canonical package stops
being importable in a cold interpreter:

```
ImportError: cannot import name 'Run' from partially initialized module
'zeroth.runtime.runs' (most likely due to a circular import)
```

The in-repo test suite cannot see this: `tests/conftest.py` imports
`zeroth.core.service.bootstrap` at collection time, so `zeroth.core` is always
warm before any test module loads. Only a library consumer — whose first
`zeroth` import may be `from zeroth.runtime.runs import Run` — hits it.
`tests/runtime/test_run_contracts.py::test_canonical_package_imports_in_a_cold_interpreter`
enforces the rule from a subprocess.

Consequently, run-model imports inside `zeroth.core` are **not** rewritten to
the canonical path yet. Each consumer is repointed when its own package leaves
`zeroth.core` — approvals in Task 13, the service APIs in Task 10, dispatch in
Task 11, and so on — because at that point it is no longer on the eager core
import path. Relocating the run models earlier would not lift this constraint
on its own: they also depend on `zeroth.core.governed` and
`zeroth.core.identity`, which trigger the same eager `__init__`.

### Graph validation

Graph validation is now seven contract-owned validators plus a composed public
entry point. `zeroth.contracts.graph.validation` holds `issues`, `references`,
`nodes`, `edges`, `tools`, `mappings`, `cycles`, and a `ContractValidator`
facade that runs them in the canonical order. None of them imports runtime,
governance, or integration code.

`GraphValidator` itself moved to `zeroth.runtime.graph_validation`, and
`zeroth.core.graph.validation` re-exports it through a module `__getattr__`.
Neither symbol appears in the canonical surface, which is not an omission:
`GraphValidator` was never a protected legacy capability. The row above records
the import location because consumers depend on it, not because a capability ID
moved.

**Why the public validator is not in `contracts`.** Two of its checks cannot
live there. Parallel-config validation resolves `reducer_ref` through the
runtime reducer registry. Capability grants resolve refs against
`zeroth.core.policy.models:Capability`, and that enum cannot move: its module
path is embedded in nine signature strings pinned by the immutable
`backend_surface_legacy.json`, the same wall documented above for the run
models. So the layer that composes contract validation with execution
validation is by definition above `contracts`, and `runtime` is the lowest
layer permitted to import contracts, governance, and runtime together.

Keeping the facade in `contracts` was the alternative. It would have left the
`parallel.errors`, `parallel.reducers`, and `policy.models` dependency
exceptions in place — relocated onto `zeroth.contracts.graph.validation.*` and
retagged, rather than removed. Composing in runtime retires all three and
leaves one edge, the shim's `zeroth.core.graph.validation` →
`zeroth.runtime.graph_validation`, tagged Task 18 next to the identical
`zeroth.core.runs` case.

**The capability seam.** The two governance rules reach the contract validators
through `CapabilityChecks`, a protocol in
`zeroth.contracts.graph.validation.capabilities`; `GraphValidator` implements
it. Injection rather than a later pass is load-bearing for behavior, not just
taste: the MCP check fires partway through a node's issues and the grant check
at the end of each agent's tool block, so running them afterwards would reorder
the report. Issue order is a contract — Studio highlights by `path`, the
console prints `message` verbatim, and the first error is the one an author
sees. `tests/contracts/graph/validation/test_characterization.py` pins codes,
paths, messages, and order for representative graphs, including one that trips
all seven validators at once.

A consumer that wants structure-only validation can use `ContractValidator`
directly: it is synchronous, needs no registry, and silently skips the
governance rules when no `CapabilityChecks` is supplied.

**Import direction.** `GraphRepository` now imports `GraphValidator` under
`TYPE_CHECKING`; it only ever named the type in annotations. The eager import
put the validation package on `zeroth.core.graph`'s own import path while the
validators import graph models straight back, which made the canonical package
uncold-importable. `tests/contracts/graph/validation/test_cold_import.py`
pins every import order from subprocesses.

### Orchestration runtime

`RuntimeOrchestrator` is now a composition facade. The work moved to six
collaborators in `zeroth.runtime.orchestration`, each holding one concern and
receiving its dependencies explicitly:

| Module | Owns |
| --- | --- |
| `driver` | the drive loop, terminal transitions, pause points, next-node planning and queueing, webhooks, artifact-TTL refresh |
| `dispatcher` | node-type resolution, agent runner wiring and restoration, retrieval, thread and template-memory resolution |
| `tool_executor` | every governed executable-unit invocation — graph step, inline code node, agent tool call |
| `parallel_executor` | fan-out, fan-in, branch governance and audit, the D-11 approval pause and its resume |
| `policy_gate` | loop guards, policy evaluation, the side-effect approval gate and its consumption |
| `audit_recorder` | every audit-repository write, plus redaction and typed-field promotion |
| `errors` | the three public exception types |

**Why the collaborators are properties, not fields.** `RuntimeOrchestrator` is a
`@dataclass(slots=True)` whose *entire* `__init__` signature — all 25 fields —
is pinned in the immutable `backend_surface_legacy.json`. No field may be added,
removed, renamed, or retyped, and `slots=True` forbids ad-hoc attributes, so a
collaborator cannot be stored at all. Each is rebuilt per access from the
orchestrator's own fields; they are frozen dataclasses, so that is free. The
same constraint is why `run_repository` could **not** be narrowed to the
`RunReader`/`RunWriter`/`CheckpointStore` protocols published in Task 5, even
though the runtime uses only five of its methods: the annotation source text
`RunRepository` is part of the pinned string.

Two dependencies point back at the facade, and both are external contracts
rather than convenience. `SubgraphExecutor.execute` takes `orchestrator=` by
keyword, and a paused child run is resumed through `resume_graph` so its run
span opens identically. Both are passed explicitly rather than reached for.

`zeroth.core.dispatch.worker` and `zeroth.core.subgraph.executor` call
`orchestrator._drive` and `orchestrator._entry_step` by name, so the facade
keeps those (and every other private helper the suite exercises) as delegating
methods. They are repointed when those packages move.

**Ordering is the contract.** The sequence of `run_repository.put` /
`write_checkpoint` / `audit_repository.write` / webhook emission is not an
implementation detail — a checkpoint written before its audit record changes
what a crashed run replays. `tests/runtime/orchestration/test_characterization.py`
pins the exact ordered call sequence for the completed, failed, rejected,
policy-denied, approval-paused and fan-out paths, and was committed green
against the pre-decomposition facade before anything moved.

**Why the canonical surface still points at the legacy modules.** The three
exception entries in `backend_surface_canonical.json` keep
`zeroth.core.orchestrator[.runtime]` as their `module`, even though the class
definitions now live in `zeroth.runtime.orchestration.errors`. That is a
decision, not an oversight, and it is the opposite of the run-models case
above — deliberately so. There the canonical package was *published*, so the
canonical entries moved with it. Orchestration's disposition row is still
`Skeleton only`: `RuntimeOrchestrator` has not moved and cannot until its pinned
capability ID is retired. Flipping only the three exceptions would have the
fixture claim a package move that has not happened. The definitions relocated
because the collaborators that raise them may not import the facade — an import
constraint, not a published relocation. The entries flip when the package does.

Nothing in the gate depends on the choice:
`test_every_canonical_symbol_imports_and_matches_its_signature` checks that the
recorded module resolves the symbol and that the signature matches; it never
compares `__module__`, and the exceptions render `<not-inspectable>` on both
sides.

#### The four dependency exceptions this task was scheduled to remove

Task 8 was scheduled to remove four `TEMPORARY_EXCEPTIONS` edges by injecting
integration collaborators. **None could be removed.** Each is retargeted onto
its new importer with a per-edge reason recorded in `src/zeroth/_architecture.py`;
the summary is:

- **`zeroth.core.execution_units` (`ExecutableUnitRunner`)** — the type is named
  in the facade's pinned `executable_unit_runner` field annotation, and the
  dependency scanner walks the AST, so even a `TYPE_CHECKING` import records the
  edge. The same wall as `RepositoryThreadStateStore` above.
- **`zeroth.core.governed.memory.models` (`MemoryScope`)** — the enum's module
  path is embedded in signature strings pinned by the immutable legacy fixture,
  the same wall as `policy.models:Capability`.
- **`zeroth.core.econ.adapter` (`InstrumentedProviderAdapter`)** — removal needs
  a provider-wrapping seam on the injected `cost_estimator`, but that field is
  typed `object | None` and duck-typed doubles are already relied on, so
  requiring a new method breaks existing callers.
- **`zeroth.core.execution_units.inline` (`build_inline_binding`)** — it
  constructs runner types, so it cannot move to contracts; removal needs a new
  run-inline-source method on `ExecutableUnitRunner`.

The last two are removable, but only by adding public methods to packages
outside this task's boundary — which is a public-interface change, not a
behavior-preserving decomposition. All four are retargeted to Task 14, which
moves the runtime packages and economics behind owned protocols and therefore
has to answer the question properly.

### Retention erasure

`RetentionErasureService` is now a composition facade. The work moved to five
collaborators in `zeroth.governance.retention`, each holding one concern and
receiving its dependencies explicitly:

| Module | Owns |
| --- | --- |
| `manifests` | building the cleanup manifest and projecting it into `ErasureResult` |
| `replay` | folding legacy retention audit entries back into claim state |
| `claims` | claim leases, `(claim_id, generation)` fencing, and the CAS writes behind them |
| `executor` | running manifest operations against the artifact store and econ plane, heartbeating the lease |
| `compatibility` | the legacy per-step retention log entries, all best-effort |
| `errors` | the two public exception types |

**Why the collaborators are properties, not fields.** The existing suite (and
`bootstrap`) reassigns `_artifact_store` and `_econ_eraser` after construction,
and monkeypatches `_replay_cleanup_state` to count legacy materializations. A
collaborator captured in `__init__` would freeze the originals and silently
ignore all of it, so each is rebuilt per access from the service's own fields;
they are frozen dataclasses, so that is free. The facade also keeps every
private helper the suite drives directly (`_release_cleanup_claim`,
`_record_operation_delta`, `_after_lock_acquired`, …) as delegating methods.

**Transaction scope is the contract.** Each fenced writer in `claims` opens
exactly one tenant-serialized transaction and does the state read, the log
append, and the CAS update inside it. `load_or_materialize`, `state_record`,
`claim`, and `repair_terminal` instead take a caller-supplied connection
because they run in the middle of the service's own transaction — re-entering
the coordinator there would deadlock on the tenant lock, and claiming outside
it would open the check-then-claim race the fence exists to close. This follows
the Task 6 precedent in `checkpoint_store`: the coordinating step stays with
the caller that holds the transaction.

**Ordering is the contract, too.** The sequence hold-check → TTL recheck →
plaintext harvest → destructive writes → `erasure_authorized` inside one
transaction, then prefix sweep → per-key deletes → econ deletion, each
bracketed by fenced deltas, then the terminal event, then the external and
database compatibility logs — is pinned by
`tests/governance/retention/test_characterization.py`, committed green against
the pre-decomposition service before anything moved.

**Why the canonical surface still points at the legacy modules.** Exactly the
orchestration case above: retention's disposition row is `Skeleton only`
(the package move is Task 13), so the canonical entries for
`RetentionErasureService`, `LegalHoldError`, and `StaleCleanupClaimError` keep
their `zeroth.core.retention[.erasure_service]` modules even though the
exception definitions now live in `zeroth.governance.retention.errors` and the
service is republished by `zeroth.governance.retention.service`. The
definitions of the exceptions relocated because the collaborators that raise
them may not import the facade — an import constraint, not a published
relocation. The service definition did not move at all: its pinned `__init__`
names `RunRepository`, the same wall as `RuntimeOrchestrator`.

**Import direction while the facade stays in `zeroth.core`.** The legacy
package resolves `RetentionErasureService` lazily and `worker.py` imports it
under `TYPE_CHECKING` only, because every extracted collaborator imports the
manifest and state models that still live in `zeroth.core.retention` — an eager
resolution there re-enters a partially initialized module the moment a cold
interpreter starts from either side.
`tests/governance/retention/test_cold_import.py` pins eight import orders from
subprocesses.

#### The three dependency exceptions this task was scheduled to remove

Task 9's tag — "decompose retention erasure behind injected cleanup adapters" —
covered three edges. **Two are removed, one is retargeted:**

- **`zeroth.core.retention.econ_eraser` → `zeroth.econ_plane.database` and
  `.instrumentation.models` — removed.** The only reason the governance domain
  imported the econ plane was that the concrete `SqlAlchemyEconEventEraser`
  lived in the retention package. The adapter moved to
  `zeroth.econ.plane.erasure` (econ → econ, always permitted); the
  `EconEventEraser` protocol stays with retention, and the erasure service
  keeps receiving the adapter by injection. The legacy module re-exports the
  class through a module `__getattr__` with no `TYPE_CHECKING` import, so no
  replacement edge exists.
- **`zeroth.core.retention.erasure_service` →
  `zeroth.integrations.persistence.runs` — retargeted to Task 18.** The
  service's pinned `__init__` names `RunRepository` in the `run_repository`
  annotation, and the dependency scanner walks the AST, so even the
  `TYPE_CHECKING` import records the edge. Narrowing to the run persistence
  protocols changes the pinned annotation text — the same wall as
  `RepositoryThreadStateStore`. Moving retention to governance in Task 13 does
  not lift it either, since governance may not import integrations; the edge
  ends when the legacy surface retires with the `zeroth.core` shell.

## Updating the canonical surface

For a moved symbol, retain its immutable legacy capability ID in the canonical
entry's `legacy_ids`, change only the canonical `module` and `name`, add the
migration row above, and run both backend contract test modules. Multiple old
IDs may map to one canonical symbol only when the implementations are proven
semantically equivalent.

### Relocating a schema-bearing service module

Service API modules need a specific three-commit order, because
`_discover_schema_models` in `tests/architecture/test_library_surface.py`
selects schema modules by *directory name* — a file counts only when its parent
directory is literally `service`. Moving a module to `zeroth/service/api/`
therefore takes it out of discovery.

The two obvious orderings both fail:

- **Fixture first** is impossible. Canonical rejects duplicate `legacy_ids`, so
  the old and new entry cannot coexist, and
  `test_every_canonical_symbol_imports_and_matches_its_signature` imports every
  entry, so canonical cannot name a module that does not exist yet.
- **Move plus discovery extension in one commit** would repoint discovery to the
  new module path while canonical still records the old one, so the production
  commit fails its own hook unless it also edits the golden fixture.

The order that works, verified on `studio_schemas`:

1. **Production move.** `git mv` the module under `zeroth/service/api/`, leave a
   re-export shim at the legacy path holding no definitions of its own, and
   point in-tree importers at the canonical location. Both paths stay
   importable, so every pinned legacy signature still resolves. Run the module's
   focused gate, the route inventory, the OpenAPI snapshot, and `tests/architecture`.
2. **Docs commit.** Repoint the canonical `module`, plus any `signature` and
   `evidence` strings embedding the old path. Leave `legacy_ids` alone — they
   name the legacy path by definition. Add the migration rows above.
3. **Final Task 10 commit only.** Extend `_discover_schema_models` to cover
   `zeroth/service/api/` and delete
   `tests/architecture/test_service_schema_relocation.py`.

Step 3 had to come last: once discovery covers the new layout, every
*subsequent* module move is discovered under its new path before its fixture is
repointed, which reinstates the deadlock. Task 10 completed this sequence for
all 22 modules; the discovery extension landed in `refactor: compose service
bootstrap`, restoring the reverse-coverage total to the exact pre-refactor 234
models (64 under `zeroth.service`), and the transitional guard was retired in
the same commit. The sequence above remains the template for any future
schema-bearing module relocation.
