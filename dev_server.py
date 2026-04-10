"""Dev server: research pipeline with agent, approval, EU, and memory."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ["ZEROTH_REGULUS__ENABLED"] = "false"
os.environ["ZEROTH_WEBHOOK__ENABLED"] = "false"
os.environ["ZEROTH_APPROVAL_SLA__ENABLED"] = "false"
os.environ["ZEROTH_DATABASE__SQLITE_PATH"] = "zeroth_dev.db"

DEPLOYMENT_REF = "research-v1"
DB_PATH = "zeroth_dev.db"
GRAPH_ID = "research-pipeline"
FORMAT_SCRIPT = Path(__file__).parent / "src" / "zeroth" / "demos" / "format_script.py"


def seed():
    """Seed: create graph with 4 nodes, publish, deploy."""
    async def _seed():
        from zeroth.contracts import ContractRegistry
        from zeroth.demos.qa_models import (
            FormatInput, FormatOutput, ResearchInput, ResearchOutput,
            SummaryInput, SummaryOutput,
        )
        from zeroth.deployments import DeploymentService, SQLiteDeploymentRepository
        from zeroth.graph import (
            AgentNode, AgentNodeData, DisplayMetadata, Edge,
            ExecutableUnitNode, ExecutableUnitNodeData, ExecutionSettings, Graph,
            HumanApprovalNode, HumanApprovalNodeData,
        )
        from zeroth.graph.repository import GraphRepository
        from zeroth.mappings.models import EdgeMapping, PassthroughMappingOperation
        from zeroth.service.bootstrap import run_migrations
        from zeroth.storage.factory import create_database
        from zeroth.config.settings import get_settings

        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        run_migrations(f"sqlite:///{DB_PATH}")

        settings = get_settings()
        database = await create_database(settings)

        # Register contracts
        cr = ContractRegistry(database)
        await cr.register(ResearchInput, name="contract://research-input")
        await cr.register(ResearchOutput, name="contract://research-output")
        await cr.register(FormatInput, name="contract://format-input")
        await cr.register(FormatOutput, name="contract://format-output")
        await cr.register(SummaryInput, name="contract://summary-input")
        await cr.register(SummaryOutput, name="contract://summary-output")

        vref = f"{GRAPH_ID}@1"
        graph = Graph(
            graph_id=GRAPH_ID,
            name="Research Pipeline",
            version=1,
            entry_step="research-agent",
            execution_settings=ExecutionSettings(
                max_total_steps=20,
                max_visits_per_node=3,
                max_total_runtime_seconds=300,
            ),
            nodes=[
                # 1. Research agent: calls OpenAI to research the question
                AgentNode(
                    node_id="research-agent",
                    graph_version_ref=vref,
                    display=DisplayMetadata(title="Research Agent"),
                    input_contract_ref="contract://research-input",
                    output_contract_ref="contract://research-output",
                    agent=AgentNodeData(
                        instruction=(
                            "You are a research assistant. Given a question, provide detailed "
                            "research findings in 2-3 sentences. Also assess your confidence "
                            "level as 'high', 'medium', or 'low'. Include the original question "
                            "in your response."
                        ),
                        model_provider="openai/gpt-4o-mini",
                        model_params={"temperature": 0.4, "max_tokens": 500},
                        retry_policy={"max_retries": 2},
                        memory_refs=["memory://research-context"],
                    ),
                ),
                # 2. Human review: pause for approval
                HumanApprovalNode(
                    node_id="human-review",
                    graph_version_ref=vref,
                    display=DisplayMetadata(title="Human Review"),
                    input_contract_ref="contract://research-output",
                    output_contract_ref="contract://format-input",
                    human_approval=HumanApprovalNodeData(
                        approval_policy_config={"allow_edits": True},
                    ),
                ),
                # 3. Formatter: executable unit (Python script)
                ExecutableUnitNode(
                    node_id="formatter",
                    graph_version_ref=vref,
                    display=DisplayMetadata(title="Formatter"),
                    input_contract_ref="contract://format-input",
                    output_contract_ref="contract://format-output",
                    executable_unit=ExecutableUnitNodeData(
                        manifest_ref="eu://formatter",
                        execution_mode="wrapped_command",
                    ),
                ),
                # 4. Summarize agent: reads memory + formatted input
                AgentNode(
                    node_id="summarize-agent",
                    graph_version_ref=vref,
                    display=DisplayMetadata(title="Summarize Agent"),
                    input_contract_ref="contract://summary-input",
                    output_contract_ref="contract://summary-output",
                    agent=AgentNodeData(
                        instruction=(
                            "You are a summarizer. Given formatted research findings, produce "
                            "a single concise sentence that captures the key insight."
                        ),
                        model_provider="openai/gpt-4o-mini",
                        model_params={"temperature": 0.2, "max_tokens": 200},
                        retry_policy={"max_retries": 1},
                        memory_refs=["memory://research-context"],
                    ),
                ),
            ],
            edges=[
                Edge(
                    edge_id="research-to-review",
                    source_node_id="research-agent",
                    target_node_id="human-review",
                    mapping=EdgeMapping(operations=[
                        PassthroughMappingOperation(source_path="question", target_path="question"),
                        PassthroughMappingOperation(source_path="findings", target_path="findings"),
                        PassthroughMappingOperation(source_path="confidence", target_path="confidence"),
                    ]),
                ),
                Edge(
                    edge_id="review-to-format",
                    source_node_id="human-review",
                    target_node_id="formatter",
                    mapping=EdgeMapping(operations=[
                        PassthroughMappingOperation(source_path="findings", target_path="findings"),
                    ]),
                ),
                Edge(
                    edge_id="format-to-summarize",
                    source_node_id="formatter",
                    target_node_id="summarize-agent",
                    mapping=EdgeMapping(operations=[
                        PassthroughMappingOperation(source_path="formatted", target_path="formatted"),
                        PassthroughMappingOperation(source_path="word_count", target_path="word_count"),
                    ]),
                ),
            ],
            deployment_settings={"tenant_id": "default"},
        )

        graph_repo = GraphRepository(database)
        saved = await graph_repo.create(graph)
        published = await graph_repo.publish(saved.graph_id, saved.version)

        dep_service = DeploymentService(
            graph_repository=graph_repo,
            deployment_repository=SQLiteDeploymentRepository(database),
            contract_registry=cr,
        )
        deployment = await dep_service.deploy(
            DEPLOYMENT_REF, published.graph_id, published.version
        )
        print(f"  Graph: {GRAPH_ID} (4 nodes, 3 edges)")
        print(f"  Deployment: {deployment.deployment_ref} v{deployment.version}")

    asyncio.run(_seed())


_cached_app = None


async def _build_app():
    """Bootstrap with runners, executable units, and memory."""
    from governai.memory.models import MemoryScope

    from zeroth.agent_runtime import AgentConfig, AgentRunner
    from zeroth.agent_runtime.models import ModelParams
    from zeroth.agent_runtime.provider import LiteLLMProviderAdapter
    from zeroth.config.settings import get_settings
    from zeroth.demos.qa_models import (
        FormatInput, FormatOutput, ResearchInput, ResearchOutput,
        SummaryInput, SummaryOutput,
    )
    from zeroth.deployments import SQLiteDeploymentRepository
    from zeroth.execution_units import (
        CommandArtifactSource, ExecutableUnitRegistry, ExecutableUnitRunner,
        ExecutionMode, InputMode, OutputMode, RunConfig, WrappedCommandUnitManifest,
    )
    from zeroth.identity import ServiceRole
    from zeroth.memory import (
        ConnectorManifest, InMemoryConnectorRegistry, KeyValueMemoryConnector,
        MemoryConnectorResolver,
    )
    from zeroth.runs import ThreadRepository
    from zeroth.service.app import create_app
    from zeroth.service.auth import ServiceAuthConfig, StaticApiKeyCredential
    from zeroth.service.bootstrap import bootstrap_service
    from zeroth.storage.factory import create_database

    settings = get_settings()
    database = await create_database(settings)

    dep_repo = SQLiteDeploymentRepository(database)
    deployment = await dep_repo.get(DEPLOYMENT_REF)
    if deployment is None:
        raise RuntimeError(f"No deployment {DEPLOYMENT_REF!r}. Run seed first.")

    # --- Agent Runners ---
    provider = LiteLLMProviderAdapter()

    research_runner = AgentRunner(
        AgentConfig(
            name="research-agent",
            instruction=(
                "You are a research assistant. Given a question, provide detailed "
                "research findings in 2-3 sentences. Assess confidence as high/medium/low. "
                "Include the original question."
            ),
            model_name="openai/gpt-4o-mini",
            input_model=ResearchInput,
            output_model=ResearchOutput,
            model_params=ModelParams(temperature=0.4, max_tokens=500),
            memory_refs=["memory://research-context"],
        ),
        provider=provider,
    )

    summarize_runner = AgentRunner(
        AgentConfig(
            name="summarize-agent",
            instruction=(
                "You are a summarizer. Given formatted research findings, produce "
                "a single concise sentence that captures the key insight."
            ),
            model_name="openai/gpt-4o-mini",
            input_model=SummaryInput,
            output_model=SummaryOutput,
            model_params=ModelParams(temperature=0.2, max_tokens=200),
            memory_refs=["memory://research-context"],
        ),
        provider=provider,
    )

    # --- Executable Unit ---
    eu_registry = ExecutableUnitRegistry()
    eu_registry.register(
        "eu://formatter",
        WrappedCommandUnitManifest(
            unit_id="formatter",
            onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
            runtime="command",
            artifact_source=CommandArtifactSource(ref=str(FORMAT_SCRIPT)),
            entrypoint_type="command",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://format-input",
            output_contract_ref="contract://format-output",
            run_config=RunConfig(command=[sys.executable, str(FORMAT_SCRIPT)]),
        ),
        input_model=FormatInput,
        output_model=FormatOutput,
    )
    eu_runner = ExecutableUnitRunner(eu_registry)

    # --- Memory ---
    memory_registry = InMemoryConnectorRegistry()
    memory_registry.register(
        "memory://research-context",
        ConnectorManifest(
            connector_type="key_value",
            scope=MemoryScope.SHARED,
            instance_id="research-shared",
        ),
        KeyValueMemoryConnector(),
    )
    thread_repo = ThreadRepository(database)
    memory_resolver = MemoryConnectorResolver(
        registry=memory_registry,
        thread_repository=thread_repo,
    )

    # --- Auth ---
    auth_config = ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="dev",
                secret="dev-key",
                subject="dev-user",
                roles=[ServiceRole.OPERATOR, ServiceRole.ADMIN, ServiceRole.REVIEWER],
                tenant_id="default",
                workspace_id=None,
            ),
        ]
    )

    # --- Bootstrap ---
    service = await bootstrap_service(
        database,
        deployment_ref=DEPLOYMENT_REF,
        agent_runners={
            "research-agent": research_runner,
            "summarize-agent": summarize_runner,
        },
        executable_unit_runner=eu_runner,
        auth_config=auth_config,
    )

    # Inject memory resolver into orchestrator
    service.orchestrator.memory_resolver = memory_resolver

    print(f"  Runners: research-agent, summarize-agent")
    print(f"  EU: formatter ({FORMAT_SCRIPT.name})")
    print(f"  Memory: research-context (shared KV)")

    return create_app(service)


def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY first")
        sys.exit(1)

    global _cached_app

    print("Seeding database...")
    seed()

    import zeroth.config.settings as _s
    _s._settings_singleton = None

    print("\nBootstrapping service...")
    _cached_app = asyncio.run(_build_app())

    import uvicorn
    print(f"\n{'=' * 60}")
    print(f"  Zeroth Research Pipeline")
    print(f"  http://localhost:8000")
    print(f"  Deployment: {DEPLOYMENT_REF}")
    print(f"{'=' * 60}")
    print()
    print("  Flow: research-agent → human-review → formatter → summarize-agent")
    print()
    print("  Step 1: Submit a research question")
    print('  curl -s -X POST http://localhost:8000/v1/runs \\')
    print('    -H "X-API-Key: dev-key" -H "Content-Type: application/json" \\')
    print('    -d \'{"input_payload": {"question": "What causes aurora borealis?"}}\'')
    print()
    print("  Step 2: Check status (will be paused_for_approval)")
    print('  curl -s http://localhost:8000/v1/runs/{RUN_ID} -H "X-API-Key: dev-key"')
    print()
    print("  Step 3: Approve the findings")
    print(f'  curl -s -X POST http://localhost:8000/v1/deployments/{DEPLOYMENT_REF}/approvals/{{APPROVAL_ID}}/resolve \\')
    print('    -H "X-API-Key: dev-key" -H "Content-Type: application/json" \\')
    print('    -d \'{"decision": "approve"}\'')
    print()
    print("  Step 4: Check final result (succeeded)")
    print('  curl -s http://localhost:8000/v1/runs/{RUN_ID} -H "X-API-Key: dev-key"')
    print()

    uvicorn.run(_cached_app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
