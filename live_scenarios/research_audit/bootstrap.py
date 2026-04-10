"""Bootstrap helpers for the live research-audit FastAPI scenario."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from governai.integrations.llm import GovernedLLM

from live_scenarios.research_audit.contracts import (
    AuditRequest,
    AuditResult,
    AuditState,
    FetchUrlInput,
    FetchUrlOutput,
    ReadFileExcerptInput,
    ReadFileExcerptOutput,
    RepoSearchInput,
    RepoSearchOutput,
    WebSearchInput,
    WebSearchOutput,
)
from live_scenarios.research_audit.tools import (
    build_read_file_excerpt_handler,
    build_tool_schema,
    fetch_url_handler,
    web_search_handler,
)
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    GovernedLLMProviderAdapter,
    RepositoryThreadResolver,
    RepositoryThreadStateStore,
    RetryPolicy,
    ToolAttachmentBridge,
    ToolAttachmentManifest,
    ToolAttachmentRegistry,
)
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderAdapter, ProviderResponse
from zeroth.core.contracts import ContractReference, ContractRegistry
from zeroth.core.contracts.errors import ContractNotFoundError, ContractVersionExistsError
from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.core.execution_units import (
    CommandArtifactSource,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
    ExecutionMode,
    InputMode,
    NativeUnitManifest,
    OutputMode,
    PythonModuleArtifactSource,
    RunConfig,
    RuntimeLanguage,
    WrappedCommandUnitManifest,
)
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    GraphRepository,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from governai.memory.models import MemoryScope

from zeroth.core.memory import (
    ConnectorManifest,
    InMemoryConnectorRegistry,
    KeyValueMemoryConnector,
    MemoryConnectorResolver,
)
from zeroth.core.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.core.service.app import create_app
from zeroth.core.service.auth import JWTBearerTokenVerifier, ServiceAuthConfig
from zeroth.core.service.bootstrap import ServiceBootstrap, bootstrap_service
from zeroth.core.storage import AsyncDatabase

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GRAPH_ID = "live-research-audit"
_DEPLOYMENT_REF = "live-research-audit"
_MEMORY_REF = "memory://investigation"


class _DeterministicToolingProvider:
    """Local fallback that forces a real tool-call path before returning JSON."""

    def __init__(self, *, repo_root: Path, file_path: Path) -> None:
        self._repo_root = repo_root
        self._file_path = file_path
        self._last_input: dict[str, Any] | None = None

    async def ainvoke(self, request) -> ProviderResponse:  # noqa: ANN001
        has_tool_result = any(
            (
                getattr(message, "role", None) == "tool"
                or (isinstance(message, dict) and message.get("role") == "tool")
                or message.__class__.__name__ == "ToolMessage"
            )
            for message in request.messages
        )
        if not has_tool_result:
            self._last_input = dict(request.metadata["input_payload"])
            return ProviderResponse(
                content=None,
                tool_calls=[
                    {
                        "id": "repo-search",
                        "name": "repo_search",
                        "args": {
                            "query": self._last_input.get("repo_query") or "bootstrap_service",
                            "repo_path": self._last_input.get("repo_path") or str(self._repo_root),
                            "max_matches": 5,
                        },
                    },
                    {
                        "id": "file-read",
                        "name": "read_file_excerpt",
                        "args": {
                            "path": self._last_input.get("file_path") or str(self._file_path),
                            "start_line": 1,
                            "end_line": 40,
                        },
                    },
                ],
            )
        payload = dict(self._last_input or {})
        return ProviderResponse(
            content={
                **payload,
                "summary": "Collected repository evidence for review.",
                "findings": ["bootstrap path should be checked for runtime wiring gaps"],
                "sources": [
                    "src/zeroth/core/service/bootstrap.py",
                    "src/zeroth/core/orchestrator/runtime.py",
                ],
                "evidence": [
                    {
                        "kind": "repo_search",
                        "title": "bootstrap_service",
                        "location": "src/zeroth/core/service/bootstrap.py",
                        "snippet": "bootstrap_service(...)",
                    },
                    {
                        "kind": "file_excerpt",
                        "title": "bootstrap excerpt",
                        "location": "src/zeroth/core/service/bootstrap.py",
                        "snippet": "def bootstrap_service(",
                    },
                ],
                "confidence": 0.65,
            }
        )


async def bootstrap_research_audit_service(
    database: AsyncDatabase,
    *,
    provider_adapters: Mapping[str, ProviderAdapter] | None = None,
    deployment_ref: str = _DEPLOYMENT_REF,
    repo_root: Path | None = None,
    strict_policy: bool = False,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
) -> ServiceBootstrap:
    """Create or load the research-audit deployment and wire live scenario runtime pieces."""

    repo_root = (repo_root or _REPO_ROOT).resolve()
    await _seed_scenario(database, deployment_ref=deployment_ref, repo_root=repo_root)

    eu_runner = _build_executable_unit_runner(repo_root)
    service = await bootstrap_service(
        database,
        deployment_ref=deployment_ref,
        agent_runners={},
        executable_unit_runner=eu_runner,
        auth_config=auth_config,
        bearer_token_verifier=bearer_token_verifier,
    )
    thread_store = RepositoryThreadStateStore(
        run_repository=service.run_repository,
        thread_repository=service.thread_repository,
    )
    memory_resolver = _build_memory_resolver(service)
    service.orchestrator.thread_resolver = RepositoryThreadResolver(service.thread_repository)
    service.orchestrator.policy_guard = _build_policy_guard(strict_policy=strict_policy)
    service.orchestrator.agent_runners = _build_agent_runners(
        provider_adapters=provider_adapters or {},
        repo_root=repo_root,
        eu_runner=eu_runner,
        thread_store=thread_store,
        memory_resolver=memory_resolver,
    )
    return service


async def bootstrap_research_audit_app(
    database: AsyncDatabase,
    *,
    provider_adapters: Mapping[str, ProviderAdapter] | None = None,
    deployment_ref: str = _DEPLOYMENT_REF,
    repo_root: Path | None = None,
    strict_policy: bool = False,
    auth_config: ServiceAuthConfig | None = None,
    bearer_token_verifier: JWTBearerTokenVerifier | None = None,
):
    """Create the FastAPI app for the research-audit deployment."""

    service = await bootstrap_research_audit_service(
        database,
        provider_adapters=provider_adapters,
        deployment_ref=deployment_ref,
        repo_root=repo_root,
        strict_policy=strict_policy,
        auth_config=auth_config,
        bearer_token_verifier=bearer_token_verifier,
    )
    return create_app(service)


async def _seed_scenario(database: AsyncDatabase, *, deployment_ref: str, repo_root: Path) -> None:
    graph_repository = GraphRepository(database)
    contract_registry = ContractRegistry(database)
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(database),
        contract_registry=contract_registry,
    )
    if await deployment_service.get(deployment_ref) is not None:
        return

    await _register_contract(contract_registry, AuditRequest, name="contract://audit-request")
    await _register_contract(contract_registry, AuditState, name="contract://audit-state")
    await _register_contract(contract_registry, AuditResult, name="contract://audit-result")
    await _register_contract(contract_registry, RepoSearchInput, name="contract://repo-search-input")
    await _register_contract(contract_registry, RepoSearchOutput, name="contract://repo-search-output")
    await _register_contract(
        contract_registry,
        ReadFileExcerptInput,
        name="contract://read-file-excerpt-input",
    )
    await _register_contract(
        contract_registry,
        ReadFileExcerptOutput,
        name="contract://read-file-excerpt-output",
    )
    await _register_contract(contract_registry, WebSearchInput, name="contract://web-search-input")
    await _register_contract(contract_registry, WebSearchOutput, name="contract://web-search-output")
    await _register_contract(contract_registry, FetchUrlInput, name="contract://fetch-url-input")
    await _register_contract(contract_registry, FetchUrlOutput, name="contract://fetch-url-output")

    graph = await graph_repository.create(_build_graph(graph_id=_GRAPH_ID, repo_root=repo_root))
    await graph_repository.publish(graph.graph_id, graph.version)
    await deployment_service.deploy(deployment_ref, graph.graph_id, graph.version)


async def _register_contract(registry: ContractRegistry, model_type: type, *, name: str) -> None:
    try:
        await registry.resolve(ContractReference(name=name))
        return
    except ContractNotFoundError:
        pass
    try:
        await registry.register(model_type, name=name)
    except ContractVersionExistsError:
        return


def _build_graph(*, graph_id: str, repo_root: Path) -> Graph:
    graph_version_ref = f"{graph_id}@1"
    del repo_root
    return Graph(
        graph_id=graph_id,
        name="Live Research Audit",
        version=1,
        entry_step="plan",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="plan",
                graph_version_ref=graph_version_ref,
                input_contract_ref="contract://audit-request",
                output_contract_ref="contract://audit-state",
                policy_bindings=["policy://scenario-plan"],
                capability_bindings=["capability://filesystem-read"],
                agent=AgentNodeData(
                    instruction=(
                        "Plan a code audit. Return strict JSON matching the output schema. "
                        "Set requires_research when repo or web evidence is needed."
                    ),
                    model_provider="provider://scenario",
                    memory_refs=[_MEMORY_REF],
                    state_persistence={"mode": "thread"},
                    thread_participation="full",
                ),
            ),
            AgentNode(
                node_id="research",
                graph_version_ref=graph_version_ref,
                input_contract_ref="contract://audit-state",
                output_contract_ref="contract://audit-state",
                agent=AgentNodeData(
                    instruction=(
                        "Investigate the audit question. Use tools when needed and return "
                        "strict JSON matching the output schema."
                    ),
                    model_provider="provider://scenario",
                    tool_refs=[
                        "repo_search",
                        "read_file_excerpt",
                        "web_search",
                        "fetch_url",
                    ],
                    memory_refs=[_MEMORY_REF],
                    state_persistence={"mode": "thread"},
                    thread_participation="full",
                ),
            ),
            ExecutableUnitNode(
                node_id="normalize_evidence",
                graph_version_ref=graph_version_ref,
                input_contract_ref="contract://audit-state",
                output_contract_ref="contract://audit-state",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://normalize_evidence",
                    execution_mode="wrapped_command",
                ),
            ),
            AgentNode(
                node_id="review",
                graph_version_ref=graph_version_ref,
                input_contract_ref="contract://audit-state",
                output_contract_ref="contract://audit-state",
                agent=AgentNodeData(
                    instruction=(
                        "Review the evidence and decide whether human approval is required. "
                        "Return strict JSON matching the output schema."
                    ),
                    model_provider="provider://scenario",
                    memory_refs=[_MEMORY_REF],
                    state_persistence={"mode": "thread"},
                    thread_participation="full",
                ),
            ),
            HumanApprovalNode(
                node_id="approval",
                graph_version_ref=graph_version_ref,
                input_contract_ref="contract://audit-state",
                output_contract_ref="contract://audit-state",
                human_approval=HumanApprovalNodeData(
                    resolution_schema_ref="schema://audit-approval",
                    approval_policy_config={"allow_edits": True},
                ),
            ),
            AgentNode(
                node_id="finalize",
                graph_version_ref=graph_version_ref,
                input_contract_ref="contract://audit-state",
                output_contract_ref="contract://audit-result",
                agent=AgentNodeData(
                    instruction=(
                        "Produce the final audit answer. Return strict JSON matching the "
                        "output schema and include approval_used."
                    ),
                    model_provider="provider://scenario",
                    memory_refs=[_MEMORY_REF],
                    state_persistence={"mode": "thread"},
                    thread_participation="full",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="plan-to-research",
                source_node_id="plan",
                target_node_id="research",
                condition=Condition(expression="payload.requires_research"),
            ),
            Edge(
                edge_id="plan-to-finalize",
                source_node_id="plan",
                target_node_id="finalize",
                condition=Condition(expression="not payload.requires_research"),
            ),
            Edge(
                edge_id="research-to-normalize",
                source_node_id="research",
                target_node_id="normalize_evidence",
            ),
            Edge(
                edge_id="normalize-to-review",
                source_node_id="normalize_evidence",
                target_node_id="review",
            ),
            Edge(
                edge_id="review-to-approval",
                source_node_id="review",
                target_node_id="approval",
                condition=Condition(expression="payload.requires_approval"),
            ),
            Edge(
                edge_id="review-to-finalize",
                source_node_id="review",
                target_node_id="finalize",
                condition=Condition(expression="not payload.requires_approval"),
            ),
            Edge(
                edge_id="approval-to-finalize",
                source_node_id="approval",
                target_node_id="finalize",
            ),
        ],
    )


def _build_executable_unit_runner(repo_root: Path) -> ExecutableUnitRunner:
    registry = ExecutableUnitRegistry()
    registry.register(
        "eu://repo_search",
        WrappedCommandUnitManifest(
            unit_id="repo-search",
            onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
            runtime=RuntimeLanguage.COMMAND,
            artifact_source=CommandArtifactSource(ref="live_scenarios.research_audit.repo_search_cli"),
            entrypoint_type="command",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://repo-search-input",
            output_contract_ref="contract://repo-search-output",
            run_config=RunConfig(
                command=[sys.executable, "-m", "live_scenarios.research_audit.repo_search_cli"],
                environment={"PYTHONPATH": str(repo_root)},
            ),
            cache_identity_fields={"repo": str(repo_root)},
        ),
        input_model=RepoSearchInput,
        output_model=RepoSearchOutput,
    )
    registry.register(
        "eu://normalize_evidence",
        WrappedCommandUnitManifest(
            unit_id="normalize-evidence",
            onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
            runtime=RuntimeLanguage.COMMAND,
            artifact_source=CommandArtifactSource(
                ref="live_scenarios.research_audit.normalize_evidence"
            ),
            entrypoint_type="command",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://audit-state",
            output_contract_ref="contract://audit-state",
            run_config=RunConfig(
                command=[sys.executable, "-m", "live_scenarios.research_audit.normalize_evidence"],
                environment={"PYTHONPATH": str(repo_root)},
            ),
            cache_identity_fields={"repo": str(repo_root)},
        ),
        input_model=AuditState,
        output_model=AuditState,
    )
    registry.register(
        "eu://read_file_excerpt",
        NativeUnitManifest(
            unit_id="read-file-excerpt",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime=RuntimeLanguage.PYTHON,
            artifact_source=PythonModuleArtifactSource(
                ref="live_scenarios.research_audit.tools:build_read_file_excerpt_handler"
            ),
            callable_ref="live_scenarios.research_audit.tools:build_read_file_excerpt_handler",
            entrypoint_type="python_callable",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://read-file-excerpt-input",
            output_contract_ref="contract://read-file-excerpt-output",
            cache_identity_fields={"repo": str(repo_root)},
        ),
        input_model=ReadFileExcerptInput,
        output_model=ReadFileExcerptOutput,
        handler=build_read_file_excerpt_handler(repo_root),
    )
    registry.register(
        "eu://web_search",
        NativeUnitManifest(
            unit_id="web-search",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime=RuntimeLanguage.PYTHON,
            artifact_source=PythonModuleArtifactSource(ref="live_scenarios.research_audit.tools:web_search_handler"),
            callable_ref="live_scenarios.research_audit.tools:web_search_handler",
            entrypoint_type="python_callable",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://web-search-input",
            output_contract_ref="contract://web-search-output",
            cache_identity_fields={
                "provider": os.getenv("LIVE_SCENARIO_SEARCH_PROVIDER", "offline")
            },
        ),
        input_model=WebSearchInput,
        output_model=WebSearchOutput,
        handler=web_search_handler,
    )
    registry.register(
        "eu://fetch_url",
        NativeUnitManifest(
            unit_id="fetch-url",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime=RuntimeLanguage.PYTHON,
            artifact_source=PythonModuleArtifactSource(ref="live_scenarios.research_audit.tools:fetch_url_handler"),
            callable_ref="live_scenarios.research_audit.tools:fetch_url_handler",
            entrypoint_type="python_callable",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://fetch-url-input",
            output_contract_ref="contract://fetch-url-output",
            cache_identity_fields={"network": "http"},
        ),
        input_model=FetchUrlInput,
        output_model=FetchUrlOutput,
        handler=fetch_url_handler,
    )
    return ExecutableUnitRunner(registry)


def _build_memory_resolver(service: ServiceBootstrap) -> MemoryConnectorResolver:
    registry = InMemoryConnectorRegistry()
    registry.register(
        _MEMORY_REF,
        ConnectorManifest(
            connector_type="key_value",
            scope=MemoryScope.THREAD,
        ),
        KeyValueMemoryConnector(),
    )
    return MemoryConnectorResolver(
        registry=registry,
        thread_repository=service.thread_repository,
    )


def _build_policy_guard(*, strict_policy: bool) -> PolicyGuard:
    capability_registry = CapabilityRegistry()
    capability_registry.register("capability://filesystem-read", Capability.FILESYSTEM_READ)

    policy_registry = PolicyRegistry()
    if strict_policy:
        policy_registry.register(
            PolicyDefinition(
                policy_id="policy://scenario-plan",
                denied_capabilities=[Capability.FILESYSTEM_READ],
            )
        )
    else:
        policy_registry.register(
            PolicyDefinition(
                policy_id="policy://scenario-plan",
                allowed_capabilities=[Capability.FILESYSTEM_READ],
            )
        )
    return PolicyGuard(
        policy_registry=policy_registry,
        capability_registry=capability_registry,
    )


def _build_agent_runners(
    *,
    provider_adapters: Mapping[str, ProviderAdapter],
    repo_root: Path,
    eu_runner: ExecutableUnitRunner,
    thread_store: RepositoryThreadStateStore,
    memory_resolver: MemoryConnectorResolver,
) -> dict[str, AgentRunner]:
    research_tool_manifests = [
        ToolAttachmentManifest(
            alias="repo_search",
            executable_unit_ref="eu://repo_search",
            permission_scope=("fs:read",),
        ),
        ToolAttachmentManifest(
            alias="read_file_excerpt",
            executable_unit_ref="eu://read_file_excerpt",
            permission_scope=("fs:read",),
        ),
        ToolAttachmentManifest(
            alias="web_search",
            executable_unit_ref="eu://web_search",
            permission_scope=("http:read",),
        ),
        ToolAttachmentManifest(
            alias="fetch_url",
            executable_unit_ref="eu://fetch_url",
            permission_scope=("http:read",),
        ),
    ]
    tool_registry = ToolAttachmentRegistry(research_tool_manifests)
    tool_bridge = ToolAttachmentBridge(tool_registry)

    async def execute_tool(binding, payload):  # noqa: ANN001
        result = await eu_runner.run_manifest_ref(binding.executable_unit_ref, payload)
        return result.output_data

    return {
        "plan": AgentRunner(
            AgentConfig(
                name="plan",
                description="Build an investigation plan for the audit request.",
                instruction=(
                    "Return only JSON matching the schema. Include repo_path, repo_query, "
                    "file_path, requires_research, requires_approval, and run_count."
                ),
                model_name=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                input_model=AuditRequest,
                output_model=AuditState,
                memory_refs=[_MEMORY_REF],
                retry_policy=RetryPolicy(max_retries=0),
            ),
            _provider_for(
                "plan",
                provider_adapters=provider_adapters,
                repo_root=repo_root,
            ),
            thread_state_store=thread_store,
            memory_resolver=memory_resolver,
        ),
        "research": AgentRunner(
            AgentConfig(
                name="research",
                description="Investigate the question with repo and web tools.",
                instruction=(
                    "Use tools when needed, then return only JSON matching the schema. "
                    "Preserve run_count and requires_approval."
                ),
                model_name=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                input_model=AuditState,
                output_model=AuditState,
                tool_attachments=research_tool_manifests,
                memory_refs=[_MEMORY_REF],
                retry_policy=RetryPolicy(max_retries=0),
            ),
            _provider_for(
                "research",
                provider_adapters=provider_adapters,
                repo_root=repo_root,
                tool_schemas={
                    "repo_search": build_tool_schema(
                        name="repo_search",
                        description="Search the repository with ripgrep.",
                        input_model=RepoSearchInput,
                    ),
                    "read_file_excerpt": build_tool_schema(
                        name="read_file_excerpt",
                        description="Read a bounded excerpt from a repository file.",
                        input_model=ReadFileExcerptInput,
                    ),
                    "web_search": build_tool_schema(
                        name="web_search",
                        description="Search the web for supporting evidence.",
                        input_model=WebSearchInput,
                    ),
                    "fetch_url": build_tool_schema(
                        name="fetch_url",
                        description="Fetch the contents of a URL.",
                        input_model=FetchUrlInput,
                    ),
                },
            ),
            thread_state_store=thread_store,
            tool_bridge=tool_bridge,
            tool_executor=execute_tool,
            granted_tool_permissions=["fs:read", "http:read"],
            memory_resolver=memory_resolver,
        ),
        "review": AgentRunner(
            AgentConfig(
                name="review",
                description="Review gathered evidence and decide if approval is required.",
                instruction=(
                    "Return only JSON matching the schema. Preserve evidence, findings, "
                    "sources, run_count, and requires_approval."
                ),
                model_name=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                input_model=AuditState,
                output_model=AuditState,
                memory_refs=[_MEMORY_REF],
                retry_policy=RetryPolicy(max_retries=0),
            ),
            _provider_for(
                "review",
                provider_adapters=provider_adapters,
                repo_root=repo_root,
            ),
            thread_state_store=thread_store,
            memory_resolver=memory_resolver,
        ),
        "finalize": AgentRunner(
            AgentConfig(
                name="finalize",
                description="Produce the final audit result.",
                instruction=(
                    "Return only JSON matching the schema. Include answer, summary, "
                    "findings, confidence, sources, approval_used, and run_count."
                ),
                model_name=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                input_model=AuditState,
                output_model=AuditResult,
                memory_refs=[_MEMORY_REF],
                retry_policy=RetryPolicy(max_retries=0),
            ),
            _provider_for(
                "finalize",
                provider_adapters=provider_adapters,
                repo_root=repo_root,
            ),
            thread_state_store=thread_store,
            memory_resolver=memory_resolver,
        ),
    }


def _provider_for(
    node_id: str,
    *,
    provider_adapters: Mapping[str, ProviderAdapter],
    repo_root: Path,
    tool_schemas: Mapping[str, dict[str, Any]] | None = None,
) -> ProviderAdapter:
    override = provider_adapters.get(node_id)
    if override is not None:
        return override

    api_key = os.getenv("OPENAI_API_KEY")
    if api_key:
        llm = GovernedLLM.from_chat_openai(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            temperature=0.2,
        )
        if tool_schemas:
            llm = llm.bind_tools(list(tool_schemas.values()), tool_choice="auto")
        return GovernedLLMProviderAdapter(llm)

    if node_id == "plan":
        return CallableProviderAdapter(_deterministic_plan_provider)
    if node_id == "research":
        return _DeterministicToolingProvider(
            repo_root=repo_root,
            file_path=repo_root / "src/zeroth/core/service/bootstrap.py",
        )
    if node_id == "review":
        return CallableProviderAdapter(_deterministic_review_provider)
    if node_id == "finalize":
        return CallableProviderAdapter(_deterministic_finalize_provider)
    raise KeyError(node_id)


def _deterministic_plan_provider(request) -> ProviderResponse:  # noqa: ANN001
    payload = dict(request.metadata["input_payload"])
    previous = request.metadata["thread_state"].get("output", {})
    run_count = int(previous.get("run_count", 0)) + 1
    return ProviderResponse(
        content={
            "question": payload["question"],
            "repo_path": payload.get("repo_path") or str(_REPO_ROOT),
            "repo_query": "bootstrap_service",
            "file_path": str(_REPO_ROOT / "src/zeroth/core/service/bootstrap.py"),
            "use_web": bool(payload.get("use_web", False)),
            "requires_research": True,
            "requires_approval": bool(payload.get("force_approval", False)),
            "approval_reason": (
                "force_approval requested" if payload.get("force_approval", False) else None
            ),
            "summary": "",
            "findings": [],
            "evidence": [],
            "sources": [],
            "confidence": 0.0,
            "run_count": run_count,
        }
    )


def _deterministic_review_provider(request) -> ProviderResponse:  # noqa: ANN001
    payload = dict(request.metadata["input_payload"])
    return ProviderResponse(
        content={
            **payload,
            "summary": payload.get("summary") or "Review complete.",
            "findings": payload.get("findings") or ["Potential runtime-wiring issue"],
            "confidence": max(float(payload.get("confidence", 0.0)), 0.88),
            "requires_approval": bool(payload.get("requires_approval", False)),
            "approval_reason": payload.get("approval_reason"),
        }
    )


def _deterministic_finalize_provider(request) -> ProviderResponse:  # noqa: ANN001
    payload = dict(request.metadata["input_payload"])
    return ProviderResponse(
        content={
            "answer": f"Audit complete for: {payload['question']}",
            "summary": payload.get("summary") or "Audit complete.",
            "findings": payload.get("findings") or [],
            "confidence": float(payload.get("confidence", 0.0)),
            "sources": payload.get("sources") or [],
            "approval_used": bool(payload.get("requires_approval", False)),
            "run_count": int(payload.get("run_count", 1)),
        }
    )
