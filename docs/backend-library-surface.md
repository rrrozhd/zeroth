# Backend Library Surface

This document is the human-readable inventory of Zeroth's protected backend
library surface before the architecture refactor begins. The executable source
of truth is the immutable `tests/contracts/fixtures/backend_surface_legacy.json`;
the current import map is `backend_surface_canonical.json` beside it.

## Baseline scope

- **880 public import bindings** across **151 modules**.
- **219 schema-model bindings** discovered from Pydantic model/schema and HTTP API modules.
- **152 public exception bindings** retained as library semantics.
- **9 optional-integration bindings** explicitly protected.
- **1 packaging entry point:** `zeroth-core = zeroth.core.cli:main`.

The inventory is the union of runtime `__all__` declarations, public package
bindings, API reference modules, imports in docs and examples, packaging entry
points, schema models, public exceptions, and optional integrations. A binding
may have more than one evidence source.

| Evidence class | Protected bindings carrying that evidence |
| --- | ---: |
| `__all__` | 557 |
| `docs` | 420 |
| `entry_point` | 1 |
| `examples` | 283 |
| `optional_integration` | 9 |
| `package_export` | 583 |
| `schema_model` | 219 |

## Contract rules

- The legacy fixture is immutable after this baseline. It protects capability
  identity and signatures without requiring legacy import paths to remain the
  canonical paths forever.
- The canonical fixture is executable import documentation. Every symbol is
  imported and every callable is passed to `inspect.signature`; opaque built-in
  exception constructors are recorded as `<not-inspectable>` only after that
  inspection raises the interpreter's expected `TypeError` or `ValueError`.
- Canonical fixture changes require a matching row in
  `docs/backend-import-migration.md` and an isolated documentation commit.
- A symbol is not removable merely because service call counts are zero.

## Optional integrations

| Import path | Signature |
| --- | --- |
| `zeroth.core.dispatch.arq_wakeup.create_arq_pool` | `(redis_settings: 'Any') -> 'Any'` |
| `zeroth.core.dispatch.arq_wakeup.enqueue_wakeup` | `(arq_pool: 'Any', run_id: 'str') -> 'None'` |
| `zeroth.core.econ.instrumentation.langgraph.LangGraphTelemetryAdapter` | `()` |
| `zeroth.core.econ.instrumentation.langgraph.instrument_langgraph_graph` | `(graph: 'Any', capability_id: 'str', implementation_id: 'str', tags: 'dict[str, Any] \| None' = None) -> 'Any'` |
| `zeroth.core.memory.chroma_connector.ChromaDBMemoryConnector` | `(client: 'chromadb.HttpClient', *, collection_prefix: 'str' = 'zeroth_memory', embedding_model: 'str \| None' = None) -> 'None'` |
| `zeroth.core.memory.elastic_connector.ElasticsearchMemoryConnector` | `(client: 'AsyncElasticsearch', *, index_prefix: 'str' = 'zeroth_memory') -> 'None'` |
| `zeroth.core.memory.pgvector_connector.PgvectorMemoryConnector` | `(conn_factory: 'Callable[[], Awaitable[psycopg.AsyncConnection]] \| str', *, table_name: 'str' = 'zeroth_memory_vectors', embedding_model: 'str \| None' = None, embedding_dimensions: 'int \| None' = None) -> 'None'` |
| `zeroth.core.memory.redis_kv.RedisKVMemoryConnector` | `(redis_client: 'aioredis.Redis', *, key_prefix: 'str' = 'zeroth:mem:kv') -> 'None'` |
| `zeroth.core.memory.redis_thread.RedisThreadMemoryConnector` | `(redis_client: 'aioredis.Redis', *, key_prefix: 'str' = 'zeroth:mem:thread') -> 'None'` |

## Complete module inventory

Each row lists every protected import binding for one module. Re-exported
objects intentionally appear at each supported package location.

| Module | Public symbols | Evidence |
| --- | --- | --- |
| `zeroth.core` | `AsyncConnection`, `AsyncDatabase`, `AsyncPostgresDatabase`, `AsyncSQLiteDatabase`, `GovernAIRedisRuntimeStores`, `Migration`, `RedisConfig`, `RedisDeploymentMode`, `SQLiteDatabase`, `build_governai_redis_runtime`, `create_database`, `docker_container_running` | `__all__`, `package_export` |
| `zeroth.core.agent_runtime` | `AgentAuditSerializer`, `AgentConfig`, `AgentContentBlockedError`, `AgentInputValidationError`, `AgentOutputValidationError`, `AgentProviderError`, `AgentRetryExhaustedError`, `AgentRunResult`, `AgentRunner`, `AgentRuntimeError`, `AgentTimeoutError`, `CachingProviderAdapter`, `CascadingProviderAdapter`, `ContentSafetyConfig`, `DeterministicProviderAdapter`, `FallbackProviderAdapter`, `HeuristicInjectionScreener`, `InMemoryResponseCache`, `InMemoryThreadStateStore`, `InjectionScreener`, `LiteLLMProviderAdapter`, `MCPServerConfig`, `ModelParams`, `OutputValidator`, `PromptAssembler`, `PromptAssembly`, `PromptConfig`, `PromptMessage`, `ProviderAdapter`, `ProviderMessage`, `ProviderRequest`, `ProviderResponse`, `ProviderTarget`, `RepositoryThreadResolver`, `RepositoryThreadStateStore`, `ResponseCache`, `RetryPolicy`, `SanitizedContent`, `ThreadResolution`, `ToolAttachmentAction`, `ToolAttachmentBinding`, `ToolAttachmentBridge`, `ToolAttachmentError`, `ToolAttachmentManifest`, `ToolAttachmentRegistry`, `ToolOutputSafetyConfig`, `ToolOutputSanitizer`, `ToolPermissionError`, `UndeclaredToolError`, `build_response_format`, `normalize_declared_tool_refs`, `wrap_untrusted` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.agent_runtime.errors` | `AgentContentBlockedError`, `AgentInputValidationError`, `AgentOutputValidationError`, `AgentProviderError`, `AgentRetryExhaustedError`, `AgentRuntimeError`, `AgentTimeoutError`, `BudgetExceededError` | `package_export` |
| `zeroth.core.agent_runtime.factory` | `AgentRunnerFactoryError` | `package_export` |
| `zeroth.core.agent_runtime.models` | `AgentConfig`, `AgentRunResult`, `ContentSafetyConfig`, `ModelParams`, `PromptAssembly`, `PromptConfig`, `PromptMessage`, `RetryPolicy`, `ToolOutputSafetyConfig` | `schema_model` |
| `zeroth.core.agent_runtime.tools` | `ToolAttachmentError`, `ToolPermissionError`, `UndeclaredToolError` | `package_export` |
| `zeroth.core.approvals` | `ApprovalDecision`, `ApprovalRecord`, `ApprovalRepository`, `ApprovalResolution`, `ApprovalService`, `ApprovalStatus`, `HumanInteractionType` | `__all__`, `docs`, `package_export` |
| `zeroth.core.approvals.models` | `ApprovalRecord`, `ApprovalResolution` | `schema_model` |
| `zeroth.core.artifacts` | `ArtifactNotFoundError`, `ArtifactReference`, `ArtifactStorageError`, `ArtifactStore`, `ArtifactStoreError`, `ArtifactStoreSettings`, `ArtifactTTLError`, `FilesystemArtifactStore`, `RedisArtifactStore`, `generate_artifact_key` | `__all__`, `package_export` |
| `zeroth.core.artifacts.errors` | `ArtifactNotFoundError`, `ArtifactStorageError`, `ArtifactStoreError`, `ArtifactTTLError` | `package_export` |
| `zeroth.core.artifacts.models` | `ArtifactReference`, `ArtifactStoreSettings` | `schema_model` |
| `zeroth.core.audit` | `ApprovalActionRecord`, `AuditContinuityReport`, `AuditContinuityVerifier`, `AuditQuery`, `AuditRedactionConfig`, `AuditRepository`, `AuditTimeline`, `AuditTimelineAssembler`, `MemoryAccessRecord`, `NodeAuditRecord`, `PayloadSanitizer`, `ToolCallRecord`, `build_summary`, `collect_policy_events`, `compute_chained_record` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.audit.coordination` | `AuditChainOrderingError` | `package_export` |
| `zeroth.core.audit.models` | `ApprovalActionRecord`, `AuditContinuityReport`, `AuditQuery`, `AuditRedactionConfig`, `AuditTimeline`, `MemoryAccessRecord`, `NodeAuditRecord`, `TokenUsage`, `ToolCallRecord` | `schema_model` |
| `zeroth.core.cli` | `main` | `entry_point` |
| `zeroth.core.conditions` | `BranchResolution`, `BranchResolver`, `ConditionBinder`, `ConditionBinding`, `ConditionContext`, `ConditionEvaluator`, `ConditionOutcome`, `ConditionResultRecorder`, `NextStepPlan`, `NextStepPlanner`, `TraversalState` | `__all__`, `docs`, `package_export` |
| `zeroth.core.conditions.errors` | `BranchResolutionError`, `ConditionEvaluationError` | `package_export` |
| `zeroth.core.conditions.models` | `BranchResolution`, `ConditionBinding`, `ConditionContext`, `ConditionOutcome`, `NextStepPlan`, `TraversalState` | `schema_model` |
| `zeroth.core.config` | `ZerothSettings`, `get_settings` | `__all__`, `package_export` |
| `zeroth.core.config.settings` | `get_settings` | `docs`, `examples` |
| `zeroth.core.context_window` | `CompactionError`, `CompactionResult`, `CompactionState`, `CompactionStrategy`, `ContextWindowError`, `ContextWindowSettings`, `ContextWindowTracker`, `LLMSummarizationStrategy`, `ObservationMaskingStrategy`, `TokenCountError`, `TruncationStrategy` | `__all__`, `package_export` |
| `zeroth.core.context_window.errors` | `CompactionError`, `ContextWindowError`, `TokenCountError` | `package_export` |
| `zeroth.core.context_window.models` | `CompactionResult`, `CompactionState`, `ContextWindowSettings` | `schema_model` |
| `zeroth.core.contracts` | `ContractNotFoundError`, `ContractReference`, `ContractRegistry`, `ContractRegistryError`, `ContractVersion`, `StepContractBinding`, `ToolContractBinding`, `validate_artifact_reference` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.contracts.errors` | `ContractNotFoundError`, `ContractRegistryError`, `ContractTypeResolutionError`, `ContractVersionExistsError` | `package_export` |
| `zeroth.core.deployments` | `Deployment`, `DeploymentError`, `DeploymentService`, `DeploymentStatus`, `SQLiteDeploymentRepository` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.deployments.models` | `Deployment` | `schema_model` |
| `zeroth.core.deployments.repository` | `DeploymentRefLineageConflictError` | `package_export` |
| `zeroth.core.deployments.service` | `DeploymentError` | `package_export` |
| `zeroth.core.dispatch` | `LeaseManager`, `RunWorker`, `WAKEUP_TASK_NAME`, `arq_settings_from_zeroth`, `create_arq_pool`, `enqueue_wakeup`, `run_arq_consumer` | `__all__`, `docs`, `package_export` |
| `zeroth.core.dispatch.arq_wakeup` | `create_arq_pool`, `enqueue_wakeup` | `optional_integration` |
| `zeroth.core.econ` | `BudgetEnforcer`, `CandidateOutcome`, `CorrectnessScorer`, `CostEstimator`, `EconReport`, `EconThresholdError`, `EquivalenceScorer`, `ExperimentReport`, `HarvestStats`, `InstrumentedProviderAdapter`, `ModelOption`, `NodeSpend`, `QualityEconomicsReport`, `RegulusClient`, `RightsizingResult`, `RunQualityVerdict`, `SpendReport`, `TenantEconomics`, `UnitEconomicsReport`, `WasteFinding`, `WasteKind`, `WasteKindTotal`, `WasteRollup`, `WasteRollupFinding`, `WorkflowEconomics`, `analyze_run`, `build_experiment_dataset`, `build_labeled_dataset`, `describe`, `quality_economics`, `read_quality_verdict`, `recommend`, `run_experiment`, `spend_opportunities`, `unit_economics`, `waste_gate`, `waste_rollup` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.econ.instrumentation` | `AutoInstrumentationConfig`, `CostProfileInput`, `ExecutionCostBreakdown`, `ExecutionEvent`, `InstrumentationClient`, `InstrumentationConfig`, `LibraryContext`, `OutcomeEvent`, `build_cost_profile_input`, `configure`, `disable_auto_instrumentation`, `enable_auto_instrumentation`, `instrument_anthropic_async_client`, `instrument_anthropic_client`, `instrument_langchain_app`, `instrument_langchain_async_runnable`, `instrument_langchain_callback_handler`, `instrument_langchain_runnable`, `instrument_langgraph_graph`, `instrument_openai_async_client`, `instrument_openai_client`, `join_key_context`, `track_execution`, `track_outcome`, `with_instrumentation` | `__all__`, `package_export` |
| `zeroth.core.econ.instrumentation.integrations` | `instrument_anthropic_async_client`, `instrument_anthropic_client`, `instrument_langchain_app`, `instrument_langchain_async_runnable`, `instrument_langchain_callback_handler`, `instrument_langchain_runnable`, `instrument_langgraph_graph`, `instrument_openai_async_client`, `instrument_openai_client` | `__all__`, `package_export` |
| `zeroth.core.econ.instrumentation.langgraph` | `LangGraphTelemetryAdapter`, `instrument_langgraph_graph` | `__all__`, `optional_integration`, `package_export` |
| `zeroth.core.econ.instrumentation.schemas` | `ExecutionEvent`, `OutcomeEvent` | `schema_model` |
| `zeroth.core.econ.models` | `CostAttribution`, `RegulusSettings` | `schema_model` |
| `zeroth.core.econ.waste` | `EconThresholdError` | `package_export` |
| `zeroth.core.eval` | `CaseResult`, `ContainsScorer`, `EvalCase`, `EvalDataset`, `EvalReport`, `EvalTarget`, `EvalThresholdError`, `ExactMatchScorer`, `JudgeVerdict`, `LLMJudgeScorer`, `PredicateScorer`, `RegexScorer`, `SchemaScorer`, `Score`, `Scorer`, `gate`, `run_eval` | `__all__`, `package_export` |
| `zeroth.core.eval.models` | `CaseResult`, `EvalCase`, `EvalDataset`, `EvalReport`, `Score` | `schema_model` |
| `zeroth.core.eval.runner` | `EvalThresholdError` | `package_export` |
| `zeroth.core.examples.quickstart` | `build_demo_graph` | `docs` |
| `zeroth.core.execution_units` | `AdmissionController`, `AdmissionResult`, `ArtifactSource`, `AuditSettings`, `BuildConfig`, `CommandArtifactSource`, `CommandRuntimeAdapter`, `DependencySpec`, `DockerSandboxConfig`, `EntryPointType`, `EnvironmentCacheManager`, `EnvironmentVariable`, `ExecutableUnitAdmissionError`, `ExecutableUnitBinding`, `ExecutableUnitError`, `ExecutableUnitExecutionError`, `ExecutableUnitManifest`, `ExecutableUnitNotFoundError`, `ExecutableUnitRegistry`, `ExecutableUnitRunResult`, `ExecutableUnitRunner`, `ExecutableUnitValidator`, `ExecutionIOError`, `ExecutionMode`, `ExtractedOutput`, `FreeformPayload`, `InjectedInput`, `InlineSourceArtifactSource`, `InlineUnitManifest`, `InputInjectionError`, `InputMode`, `ManifestIntegrityRecord`, `ManifestValidationError`, `NativeUnitManifest`, `OutputConversionError`, `OutputExtractionError`, `OutputMode`, `ProjectArchiveArtifactSource`, `ProjectUnitManifest`, `PythonModuleArtifactSource`, `PythonRuntimeAdapter`, `ResourceConstraints`, `ResourceLimits`, `RunConfig`, `RuntimeAdapter`, `RuntimeLanguage`, `SandboxBackendMode`, `SandboxBackendUnavailableError`, `SandboxConfig`, `SandboxEnvironment`, `SandboxExecutionResult`, `SandboxManager`, `SandboxPolicyViolationError`, `SandboxStrictnessMode`, `SandboxTimeoutError`, `ValidationCode`, `WrappedCommandUnitManifest`, `build_docker_resource_flags`, `build_inline_binding`, `build_inline_manifest`, `build_sandbox_environment`, `compute_environment_cache_key`, `compute_manifest_digest`, `convert_output`, `docker_container_running`, `extract_output`, `inject_input`, `inline_source_digest` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.execution_units.constraints` | `ResourceConstraints`, `build_docker_resource_flags` | `__all__` |
| `zeroth.core.execution_units.errors` | `ManifestValidationError`, `UnsupportedRuntimeAdapterError` | `package_export` |
| `zeroth.core.execution_units.integrity` | `AdmissionController`, `AdmissionResult`, `ManifestIntegrityRecord`, `compute_manifest_digest` | `__all__` |
| `zeroth.core.execution_units.io` | `ExecutionIOError`, `InputInjectionError`, `OutputConversionError`, `OutputExtractionError` | `package_export` |
| `zeroth.core.execution_units.models` | `ArtifactSource`, `AuditSettings`, `BuildConfig`, `CommandArtifactSource`, `DependencySpec`, `EnvironmentVariable`, `ExecutableUnitManifestBase`, `InlineSourceArtifactSource`, `InlineUnitManifest`, `NativeUnitManifest`, `ProjectArchiveArtifactSource`, `ProjectUnitManifest`, `PythonModuleArtifactSource`, `ResourceLimits`, `RunConfig`, `WrappedCommandUnitManifest` | `docs`, `schema_model` |
| `zeroth.core.execution_units.runner` | `ExecutableUnitAdmissionError`, `ExecutableUnitBinding`, `ExecutableUnitError`, `ExecutableUnitExecutionError`, `ExecutableUnitInputError`, `ExecutableUnitNotFoundError`, `ExecutableUnitRegistry`, `ExecutableUnitRunResult`, `ExecutableUnitRunner` | `__all__`, `package_export` |
| `zeroth.core.execution_units.sandbox` | `DockerSandboxConfig`, `EnvironmentCacheManager`, `SandboxBackendMode`, `SandboxBackendUnavailableError`, `SandboxConfig`, `SandboxEnvironment`, `SandboxExecutionResult`, `SandboxManager`, `SandboxPolicyViolationError`, `SandboxStrictnessMode`, `SandboxTimeoutError`, `build_sandbox_environment`, `compute_environment_cache_key`, `docker_container_running` | `__all__`, `package_export` |
| `zeroth.core.execution_units.sidecar_client` | `SandboxSidecarClient` | `__all__` |
| `zeroth.core.governed` | `RunState`, `RunStatus`, `Tool` | `__all__`, `package_export` |
| `zeroth.core.governed.memory.models` | `MemoryEntry`, `MemoryScope` | `docs`, `examples`, `schema_model` |
| `zeroth.core.governed.runtime` | `RedisInterruptStore`, `RedisRunStore` | `__all__`, `package_export` |
| `zeroth.core.governed.runtime.run_store` | `StateConcurrencyError` | `package_export` |
| `zeroth.core.governed.tools.base` | `CLIToolError`, `CLIToolOutputError`, `CLIToolProcessError`, `CLIToolTimeoutError`, `ToolError`, `ToolExecutionError`, `ToolValidationError` | `package_export` |
| `zeroth.core.graph` | `AgentNode`, `AgentNodeData`, `AgentToolBinding`, `Condition`, `DisplayMetadata`, `Edge`, `EntrypointNode`, `EntrypointNodeData`, `ExecutableUnitNode`, `ExecutableUnitNodeData`, `ExecutionSettings`, `Graph`, `GraphRepository`, `GraphStatus`, `HumanApprovalNode`, `HumanApprovalNodeData`, `Node`, `RetrievalNode`, `RetrievalNodeData`, `SubgraphNode`, `SubgraphNodeData`, `TemplateMemoryBinding`, `ToolArgument` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.graph.errors` | `GraphLifecycleError` | `package_export` |
| `zeroth.core.graph.models` | `AgentNode`, `AgentNodeData`, `AgentToolBinding`, `Condition`, `DisplayMetadata`, `Edge`, `EntrypointNode`, `EntrypointNodeData`, `ExecutableUnitNode`, `ExecutableUnitNodeData`, `ExecutionSettings`, `Graph`, `HumanApprovalNode`, `HumanApprovalNodeData`, `NodeBase`, `RetrievalNode`, `RetrievalNodeData`, `SubgraphNode`, `TemplateMemoryBinding`, `ToolArgument` | `schema_model` |
| `zeroth.core.graph.validation_errors` | `GraphValidationError` | `package_export` |
| `zeroth.core.guardrails` | `BlocklistFilter`, `ContentFilter`, `ContentFinding`, `ContentGuardrail`, `DeadLetterManager`, `GuardrailConfig`, `GuardrailOutcome`, `PIIFilter`, `QuotaEnforcer`, `TokenBucketRateLimiter` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.http` | `AuthType`, `CircuitBreaker`, `CircuitBreakerRegistry`, `CircuitOpenError`, `CircuitState`, `EndpointConfig`, `HttpCallRecord`, `HttpClientError`, `HttpClientSettings`, `HttpRateLimitError`, `HttpRetryExhaustedError`, `InMemoryTokenBucket`, `ResilientHttpClient`, `redact_url` | `__all__`, `package_export` |
| `zeroth.core.http.errors` | `CircuitOpenError`, `HttpClientError`, `HttpRateLimitError`, `HttpRetryExhaustedError` | `package_export` |
| `zeroth.core.http.models` | `EndpointConfig`, `HttpCallRecord`, `HttpClientSettings` | `schema_model` |
| `zeroth.core.identity` | `ActorIdentity`, `AuthMethod`, `AuthenticatedPrincipal`, `PrincipalScope`, `ServiceRole` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.identity.models` | `ActorIdentity`, `AuthenticatedPrincipal`, `PrincipalScope` | `schema_model` |
| `zeroth.core.mappings` | `ConstantMappingOperation`, `DefaultMappingOperation`, `EdgeMapping`, `MappingExecutionError`, `MappingExecutor`, `MappingOperation`, `MappingValidationError`, `MappingValidator`, `PassthroughMappingOperation`, `RenameMappingOperation`, `TransformMappingOperation` | `__all__`, `docs`, `package_export` |
| `zeroth.core.mappings.errors` | `MappingExecutionError`, `MappingValidationError` | `package_export` |
| `zeroth.core.mappings.models` | `ConstantMappingOperation`, `DefaultMappingOperation`, `EdgeMapping`, `MappingOperationBase`, `PassthroughMappingOperation`, `RenameMappingOperation`, `TransformMappingOperation` | `examples`, `schema_model` |
| `zeroth.core.memory` | `ConnectorManifest`, `InMemoryConnectorRegistry`, `KeyValueMemoryConnector`, `MemoryConnectorResolver`, `ResolvedMemoryBinding`, `RunEphemeralMemoryConnector`, `ThreadMemoryConnector`, `register_memory_connectors` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.memory.capability_guard` | `CapabilityEnforcingMemoryConnector` | `__all__` |
| `zeroth.core.memory.chroma_connector` | `ChromaDBMemoryConnector` | `examples`, `optional_integration` |
| `zeroth.core.memory.elastic_connector` | `ElasticsearchMemoryConnector` | `optional_integration` |
| `zeroth.core.memory.models` | `ConnectorManifest`, `ResolvedMemoryBinding` | `schema_model` |
| `zeroth.core.memory.pgvector_connector` | `PgvectorMemoryConnector` | `optional_integration` |
| `zeroth.core.memory.redis_kv` | `RedisKVMemoryConnector` | `optional_integration` |
| `zeroth.core.memory.redis_thread` | `RedisThreadMemoryConnector` | `examples`, `optional_integration` |
| `zeroth.core.memory.tenant_scoped` | `TenantScopeError` | `package_export` |
| `zeroth.core.observability` | `MetricsCollector`, `configure_tracing`, `get_correlation_id`, `new_correlation_id`, `set_correlation_id`, `start_span` | `__all__`, `examples`, `package_export` |
| `zeroth.core.orchestrator` | `NodeDispatcherError`, `OrchestratorError`, `RuntimeOrchestrator` | `__all__`, `docs`, `package_export` |
| `zeroth.core.orchestrator.runtime` | `MemoryBindingResolutionError`, `NodeDispatcherError`, `OrchestratorError` | `package_export` |
| `zeroth.core.parallel` | `BranchContext`, `BranchError`, `BranchResult`, `FanInResult`, `FanOutValidationError`, `GlobalStepTracker`, `ParallelConfig`, `ParallelExecutionError`, `ParallelExecutor`, `ParallelStepLimitError` | `__all__`, `package_export` |
| `zeroth.core.parallel.errors` | `BranchApprovalPauseSignal`, `BranchError`, `FanOutValidationError`, `MergeStrategyError`, `MergeStrategyValidationError`, `ParallelExecutionError`, `ParallelStepLimitError`, `ReducerRefValidationError` | `package_export` |
| `zeroth.core.parallel.models` | `ParallelConfig` | `schema_model` |
| `zeroth.core.policy` | `Capability`, `CapabilityDeniedError`, `CapabilityRegistry`, `EnforcementResult`, `PolicyDecision`, `PolicyDefinition`, `PolicyGuard`, `PolicyRegistry`, `apply_secret_policy`, `default_capability_registry`, `parse_effective_capabilities`, `require_capabilities` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.policy.errors` | `CapabilityDeniedError`, `parse_effective_capabilities`, `require_capabilities` | `__all__`, `package_export` |
| `zeroth.core.policy.models` | `Capability`, `EnforcementResult`, `PolicyDefinition` | `docs`, `schema_model` |
| `zeroth.core.policy.registry` | `CapabilityRegistry`, `PolicyRegistry` | `docs` |
| `zeroth.core.rag` | `IngestionReport`, `SourceDocument`, `chunk_text`, `ingest_documents` | `__all__`, `package_export` |
| `zeroth.core.retention` | `EconEventEraser`, `ErasureResult`, `LegalHold`, `LegalHoldError`, `LegalHoldRepository`, `RetentionAuditLogRepository`, `RetentionErasureService`, `RetentionPolicy`, `RetentionPolicyRepository`, `RetentionPurgeWorker`, `SqlAlchemyEconEventEraser`, `TenantHolds` | `__all__`, `package_export` |
| `zeroth.core.retention.erasure_service` | `LegalHoldError`, `StaleCleanupClaimError` | `package_export` |
| `zeroth.core.retention.models` | `LegalHold`, `RetentionPolicy` | `schema_model` |
| `zeroth.core.runs` | `Run`, `RunConditionResult`, `RunFailureState`, `RunHistoryEntry`, `RunRepository`, `RunState`, `RunStatus`, `Thread`, `ThreadMemoryBinding`, `ThreadRepository`, `ThreadStatus` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.runs.models` | `Run`, `RunConditionResult`, `RunFailureState`, `RunHistoryEntry`, `Thread`, `ThreadMemoryBinding` | `schema_model` |
| `zeroth.core.sandbox_sidecar` | `app` | `__all__` |
| `zeroth.core.sandbox_sidecar.app` | `app` | `__all__` |
| `zeroth.core.sandbox_sidecar.executor` | `SidecarExecutor` | `__all__` |
| `zeroth.core.sandbox_sidecar.models` | `SidecarExecuteRequest`, `SidecarExecuteResponse`, `SidecarHealthResponse`, `SidecarStatusResponse` | `__all__`, `schema_model` |
| `zeroth.core.secrets` | `EnvSecretProvider`, `SecretProvider`, `SecretProviderConfigError`, `SecretRedactor`, `SecretResolutionError`, `SecretResolver`, `VaultSecretProvider`, `build_secret_provider`, `normalize_secret_name`, `resolve_async`, `resolve_many_async`, `resolve_secret_async` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.secrets.factory` | `SecretProviderConfigError`, `build_secret_provider` | `__all__`, `package_export` |
| `zeroth.core.secrets.provider` | `EnvSecretProvider`, `SecretProvider`, `SecretResolutionError`, `SecretResolver`, `normalize_secret_name`, `resolve_async`, `resolve_many_async`, `resolve_secret_async` | `__all__`, `package_export` |
| `zeroth.core.secrets.redaction` | `SecretRedactor` | `__all__` |
| `zeroth.core.secrets.vault` | `VaultSecretProvider` | `__all__` |
| `zeroth.core.service` | `DeploymentBootstrapError`, `ServiceBootstrap`, `bootstrap_app`, `bootstrap_service`, `create_app` | `__all__`, `docs`, `package_export` |
| `zeroth.core.service.admin_api` | `AdminRunListResponse` | `schema_model` |
| `zeroth.core.service.app` | `HealthResponse`, `create_app` | `docs`, `examples`, `schema_model` |
| `zeroth.core.service.approval_api` | `ApprovalResolutionRequest`, `ApprovalResolutionResponse` | `schema_model` |
| `zeroth.core.service.audit_api` | `AttestationVerificationResponse`, `AuditRecordListResponse`, `AuditTimelineResponse`, `AuditVerificationResponse`, `DeploymentAttestationResponse`, `DeploymentEvidenceResponse`, `EvidenceSummaryResponse`, `RunEvidenceResponse`, `VerifyChainRequest` | `schema_model` |
| `zeroth.core.service.auth` | `AuthenticationError`, `BearerTokenConfig`, `ServiceAuthConfig`, `StaticApiKeyCredential` | `docs`, `examples`, `package_export` |
| `zeroth.core.service.bootstrap` | `DeploymentBootstrapError`, `ServiceBootstrap`, `bootstrap_service`, `run_migrations` | `docs`, `examples`, `package_export` |
| `zeroth.core.service.connector_api` | `ConnectorCreateRequest`, `ConnectorSummaryResponse`, `ConnectorTestResponse`, `ConnectorUpdateRequest` | `schema_model` |
| `zeroth.core.service.contracts_api` | `DeploymentResultErrorStateSchemaResponse`, `DeploymentVersionMetadataResponse`, `PublicContractSchemaResponse` | `schema_model` |
| `zeroth.core.service.cost_api` | `DeploymentCostResponse`, `TenantBudgetRequest`, `TenantCostResponse` | `schema_model` |
| `zeroth.core.service.deployment_api` | `CreateDeploymentRequest`, `DeploymentSummaryResponse`, `RollbackDeploymentRequest` | `schema_model` |
| `zeroth.core.service.econ_analytics_api` | `QualityVerdictRequest` | `schema_model` |
| `zeroth.core.service.manifest_api` | `ManifestSummaryResponse` | `schema_model` |
| `zeroth.core.service.retention_api` | `ErasureRequestBody`, `ErasureResponse`, `ErasureRunResult`, `LegalHoldBody`, `LegalHoldResponse`, `RetentionPolicyBody`, `RetentionPolicyResponse` | `schema_model` |
| `zeroth.core.service.rightsizing_api` | `ExperimentRequest`, `RightsizingRequest` | `schema_model` |
| `zeroth.core.service.run_api` | `ApprovalPausedState`, `RunInvocationRequest`, `RunInvocationResponse`, `RunStatusResponse` | `schema_model` |
| `zeroth.core.service.template_api` | `CreateTemplateRequest`, `TemplateListResponse`, `TemplateResponse` | `schema_model` |
| `zeroth.core.service.webhook_api` | `CreateSubscriptionRequest`, `WebhookDeadLetterListResponse`, `WebhookDeadLetterResponse`, `WebhookSubscriptionListResponse`, `WebhookSubscriptionResponse` | `schema_model` |
| `zeroth.core.signing` | `Ed25519Signer`, `EnvHmacSigner`, `NullSigner`, `SigningConfigError`, `SigningKeyProvider`, `build_signing_provider`, `build_signing_provider_async`, `sign_digest`, `signable_bytes`, `verify_digest` | `__all__`, `package_export` |
| `zeroth.core.signing.provider` | `SigningConfigError` | `package_export` |
| `zeroth.core.storage` | `AsyncConnection`, `AsyncDatabase`, `AsyncPostgresDatabase`, `AsyncSQLiteDatabase`, `EncryptedField`, `GovernAIRedisRuntimeStores`, `Migration`, `RedisConfig`, `RedisDeploymentMode`, `SQLiteDatabase`, `build_governai_redis_runtime`, `create_database`, `docker_container_running`, `ensure_and_lock_row` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.storage.async_sqlite` | `AsyncSQLiteDatabase` | `docs` |
| `zeroth.core.storage.database` | `CoordinationTimeoutError` | `package_export` |
| `zeroth.core.storage.factory` | `create_database` | `docs`, `examples` |
| `zeroth.core.subgraph` | `SubgraphCycleError`, `SubgraphDepthLimitError`, `SubgraphError`, `SubgraphExecutionError`, `SubgraphExecutor`, `SubgraphNodeData`, `SubgraphResolutionError` | `__all__`, `package_export` |
| `zeroth.core.subgraph.errors` | `SubgraphCycleError`, `SubgraphDepthLimitError`, `SubgraphError`, `SubgraphExecutionError`, `SubgraphResolutionError` | `package_export` |
| `zeroth.core.subgraph.models` | `SubgraphNodeData` | `schema_model` |
| `zeroth.core.templates` | `DEFAULT_SECRET_PATTERNS`, `PromptTemplate`, `TemplateError`, `TemplateNotFoundError`, `TemplateReference`, `TemplateRegistry`, `TemplateRenderError`, `TemplateRenderResult`, `TemplateRenderer`, `TemplateSyntaxValidationError`, `TemplateVersionExistsError`, `identify_secret_variables`, `redact_rendered_prompt` | `__all__`, `package_export` |
| `zeroth.core.templates.errors` | `TemplateError`, `TemplateNotFoundError`, `TemplateRenderError`, `TemplateSyntaxValidationError`, `TemplateVersionExistsError` | `package_export` |
| `zeroth.core.templates.models` | `PromptTemplate`, `TemplateReference`, `TemplateRenderResult` | `schema_model` |
| `zeroth.core.webhooks` | `DeliveryStatus`, `EscalationAction`, `WebhookDeadLetter`, `WebhookDelivery`, `WebhookDeliveryWorker`, `WebhookEventPayload`, `WebhookEventType`, `WebhookRepository`, `WebhookService`, `WebhookSubscription`, `sign_payload` | `__all__`, `docs`, `examples`, `package_export` |
| `zeroth.core.webhooks.delivery` | `WebhookDeliveryWorker` | `examples` |
| `zeroth.core.webhooks.models` | `WebhookDeadLetter`, `WebhookDelivery`, `WebhookEventPayload`, `WebhookSubscription` | `schema_model` |
| `zeroth.core.webhooks.service` | `WebhookService` | `examples` |
| `zeroth.econ_plane` | `main` | `__all__`, `package_export` |
| `zeroth.econ_plane.auth.schemas` | `LoginRequest`, `TokenResponse`, `UserClaims` | `schema_model` |
| `zeroth.econ_plane.capabilities.schemas` | `CapabilityCreate`, `CapabilityDetail`, `CapabilityOut`, `ConfidenceGateConfig`, `DeploymentCreate`, `DeploymentOut`, `ExperimentCreate`, `ExperimentOut`, `ImplementationCreate`, `ImplementationOut`, `ValuationConfig` | `schema_model` |
| `zeroth.econ_plane.common.schemas` | `APIMessage` | `schema_model` |
| `zeroth.econ_plane.connectors.schemas` | `ConnectorConfigOut`, `ConnectorConfigRequest`, `ConnectorEventEnvelope`, `ConnectorHealthResult`, `ConnectorOutboxOut`, `ConnectorSendResult`, `ConnectorStatusOut`, `RetryOutboxResponse` | `schema_model` |
| `zeroth.econ_plane.costing.schemas` | `CostEstimateOut`, `CostProfileCreate`, `CostProfileOut`, `PricingCatalogCreate` | `schema_model` |
| `zeroth.econ_plane.counterfactual.schemas` | `EvaluationRunRequest`, `ValueEstimateOut` | `schema_model` |
| `zeroth.econ_plane.dashboard.schemas` | `CapabilityRankingRow`, `CapabilityValueRow`, `ConfidenceGateStatus`, `DataQualityMix`, `ImplementationCompareRow`, `KPIResponse`, `PolicyTimelineRow`, `TrendPoint` | `schema_model` |
| `zeroth.econ_plane.enforcement.schemas` | `BudgetStatusOut`, `DecisionRequest`, `EnforcementActionCreate`, `EnforcementActionOut`, `PolicyActionOut`, `TenantBudgetUpsert` | `schema_model` |
| `zeroth.econ_plane.instrumentation.schemas` | `ExecutionEventCreate`, `IngestResult`, `OutcomeBatchIngestRequest`, `OutcomeEventCreate`, `OutcomeQueryResponse` | `schema_model` |
| `zeroth.econ_plane.performance.schemas` | `CapabilityPerformance`, `PerformanceSummary` | `schema_model` |
| `zeroth.econ_plane.reconciliation.schemas` | `GroundTruthCostIn`, `GroundTruthImportRequest` | `schema_model` |
| `zeroth.econ_plane.reconciliation.service` | `add_ground_truth_rows`, `compute_calibration_summary` | `__all__` |
| `zeroth.econ_plane.statistics.schemas` | `CalibrationSummary`, `IntervalEstimate` | `schema_model` |
