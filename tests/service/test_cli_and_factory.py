"""Tests for the zeroth-core CLI seed path and the agent-runner factory."""

from __future__ import annotations

import pytest

from zeroth.contracts.graph import (
    DisplayMetadata,
    Edge,
    Graph,
    GraphRepository,
    SubgraphNode,
    SubgraphNodeData,
)
from zeroth.contracts.registry import ContractRegistry, contract_scope_context
from zeroth.runtime.agents.factory import (
    AgentRunnerFactoryError,
    build_agent_runners,
)
from zeroth.runtime.agents.thread_store import RepositoryThreadStateStore
from zeroth.runtime.agents.mcp import MCPServerConfig
from zeroth.service.bootstrap.factory import build_runners_for_deployment
from zeroth.service.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.service.demo import (
    DEMO_GRAPH_ID,
    DEMO_INPUT_CONTRACT,
    DEMO_OUTPUT_CONTRACT,
    DemoAnswer,
    DemoQuestion,
    build_hello_graph,
    seed_demo,
)
from tests.service.helpers import agent_graph, deploy_service
from tests.service.helpers import RunInputPayload


async def test_seed_demo_creates_published_graph_and_deployment(sqlite_db):
    deployment = await seed_demo(sqlite_db, deployment_ref="default")

    assert deployment.deployment_ref == "default"
    assert deployment.graph_id == DEMO_GRAPH_ID
    assert deployment.graph_version_ref == f"{DEMO_GRAPH_ID}@1"

    registry = ContractRegistry.for_default_compatibility(sqlite_db)
    assert await registry.latest_version(DEMO_INPUT_CONTRACT) == 1
    assert await registry.latest_version(DEMO_OUTPUT_CONTRACT) == 1


async def test_seed_demo_is_idempotent(sqlite_db):
    first = await seed_demo(sqlite_db, deployment_ref="default")
    second = await seed_demo(sqlite_db, deployment_ref="default")

    assert second.deployment_ref == first.deployment_ref
    assert second.graph_version_ref == first.graph_version_ref


async def test_factory_builds_runner_from_agent_node_data(sqlite_db):
    registry = ContractRegistry.for_default_compatibility(sqlite_db)
    await registry.register(DemoQuestion, name=DEMO_INPUT_CONTRACT)
    await registry.register(DemoAnswer, name=DEMO_OUTPUT_CONTRACT)

    graph = build_hello_graph(model="anthropic/claude-sonnet-5")
    runners = await build_agent_runners(graph, registry)

    assert set(runners) == {"agent"}
    config = runners["agent"].config
    assert config.model_name == "anthropic/claude-sonnet-5"
    assert config.input_model is DemoQuestion
    assert config.output_model is DemoAnswer
    assert config.instruction


async def test_factory_builds_runner_from_explicitly_versioned_contract_refs(sqlite_db):
    registry = ContractRegistry.for_default_compatibility(sqlite_db)
    await registry.register(DemoQuestion, name=DEMO_INPUT_CONTRACT)
    await registry.register(DemoAnswer, name=DEMO_OUTPUT_CONTRACT)
    graph = build_hello_graph(model="openai/gpt-4o-mini")
    node = graph.nodes[0]
    graph.nodes[0] = node.model_copy(
        update={
            "input_contract_ref": f"{DEMO_INPUT_CONTRACT}@1",
            "output_contract_ref": f"{DEMO_OUTPUT_CONTRACT}@1",
        }
    )

    runners = await build_agent_runners(graph, registry)

    assert runners["agent"].config.input_model is DemoQuestion
    assert runners["agent"].config.output_model is DemoAnswer


async def test_factory_threads_trusted_provider_base_url_to_default_adapter(sqlite_db):
    class _Secrets:
        def resolve_secret(self, logical_name, *, tenant_id=None, deployment_ref=None):
            assert logical_name == "llm.openai"
            return "invalid-evaluation-value"

    registry = ContractRegistry.for_default_compatibility(sqlite_db)
    await registry.register(DemoQuestion, name=DEMO_INPUT_CONTRACT)
    await registry.register(DemoAnswer, name=DEMO_OUTPUT_CONTRACT)
    graph = build_hello_graph(model="openai/gpt-4o-mini")

    runners = await build_agent_runners(
        graph,
        registry,
        secret_provider=_Secrets(),
        tenant_id="evaluation-tenant",
        allow_env_fallback=False,
        llm_base_url_map={"openai": "http://127.0.0.1:18124/v1"},
    )

    provider = runners["agent"].provider
    client = provider._get_client("openai/gpt-4o-mini")
    assert client.api_base == "http://127.0.0.1:18124/v1"


async def test_factory_preserves_declared_mcp_servers(sqlite_db):
    registry = ContractRegistry.for_default_compatibility(sqlite_db)
    await registry.register(DemoQuestion, name=DEMO_INPUT_CONTRACT)
    await registry.register(DemoAnswer, name=DEMO_OUTPUT_CONTRACT)

    graph = build_hello_graph(model="anthropic/claude-sonnet-5")
    graph.nodes[0].agent.mcp_servers = [
        {"name": "files", "command": "npx", "args": ["mcp-files"]},
        {"name": "search", "command": "python", "args": ["-m", "mcp_search"]},
    ]
    runners = await build_agent_runners(graph, registry)

    config = runners["agent"].config
    assert [server.name for server in config.mcp_servers] == ["files", "search"]
    assert all(isinstance(server, MCPServerConfig) for server in config.mcp_servers)


async def test_factory_raises_actionable_error_for_unregistered_contract(sqlite_db):
    registry = ContractRegistry.for_default_compatibility(sqlite_db)
    graph = build_hello_graph()

    with pytest.raises(AgentRunnerFactoryError, match="agent"):
        await build_agent_runners(graph, registry)


async def test_build_runners_for_deployment_roundtrip(sqlite_db):
    await seed_demo(sqlite_db, deployment_ref="default")

    runners = await build_runners_for_deployment(sqlite_db, "default")

    assert runners is not None
    assert set(runners) == {"agent"}


async def test_deployment_runners_use_repository_backed_thread_state(sqlite_db):
    """Serve-time runners must not lose compacted context on process restart."""
    await seed_demo(sqlite_db, deployment_ref="durable-thread-state")

    runners = await build_runners_for_deployment(sqlite_db, "durable-thread-state")

    assert runners is not None
    assert isinstance(runners["agent"].thread_state_store, RepositoryThreadStateStore)


async def test_build_runners_for_missing_deployment_returns_none(sqlite_db):
    assert await build_runners_for_deployment(sqlite_db, "nope") is None


async def test_build_runners_resolves_non_default_deployment_in_exact_scope(sqlite_db):
    _, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="tenant-runner-graph"),
        deployment_ref="tenant-runner-deployment",
        tenant_id="tenant-a",
        workspace_id="workspace-a",
    )

    assert (
        await build_runners_for_deployment(
            sqlite_db,
            deployment.deployment_ref,
            tenant_id=deployment.tenant_id,
            workspace_id="other-workspace",
        )
        is None
    )
    runners = await build_runners_for_deployment(
        sqlite_db,
        deployment.deployment_ref,
        tenant_id=deployment.tenant_id,
        workspace_id=deployment.workspace_id,
    )

    assert runners is not None
    assert set(runners) == {"agent-step"}


async def test_build_runners_recurses_through_scoped_deployed_subgraphs(sqlite_db):
    tenant_id = "tenant-recursive"
    workspace_id = "workspace-recursive"
    registry = ContractRegistry.scoped(
        sqlite_db,
        contract_scope_context(tenant_id, workspace_id),
    )
    await registry.register(RunInputPayload, name="contract://input")
    await registry.register(RunInputPayload, name="contract://output")
    graph_repository = GraphRepository(sqlite_db)
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=registry,
    )

    async def deploy(graph: Graph, ref: str) -> None:
        scoped = graph.model_copy(update={"tenant_id": tenant_id, "workspace_id": workspace_id})
        saved = await graph_repository.create(
            scoped,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        await graph_repository.publish(
            saved.graph_id,
            saved.version,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        await deployment_service.deploy(
            ref,
            saved.graph_id,
            saved.version,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

    await deploy(agent_graph(graph_id="grand-graph", node_id="grand-agent"), "grand-dep")
    child_agent = agent_graph(graph_id="child-graph", node_id="child-agent").nodes[0]
    await deploy(
        Graph(
            graph_id="child-graph",
            name="Child",
            entry_step="child-agent",
            nodes=[
                child_agent,
                SubgraphNode(
                    node_id="nested",
                    graph_version_ref="child-graph@1",
                    display=DisplayMetadata(title="Nested"),
                    input_contract_ref="contract://input",
                    output_contract_ref="contract://output",
                    subgraph=SubgraphNodeData(graph_ref="grand-dep"),
                )
            ],
            edges=[
                Edge(
                    edge_id="child-to-nested",
                    source_node_id="child-agent",
                    target_node_id="nested",
                )
            ],
        ),
        "child-dep",
    )
    await deploy(
        Graph(
            graph_id="parent-graph",
            name="Parent",
            entry_step="child",
            nodes=[
                SubgraphNode(
                    node_id="child",
                    graph_version_ref="parent-graph@1",
                    display=DisplayMetadata(title="Child"),
                    input_contract_ref="contract://input",
                    output_contract_ref="contract://output",
                    subgraph=SubgraphNodeData(graph_ref="child-dep"),
                )
            ],
            edges=[],
        ),
        "parent-dep",
    )

    runners = await build_runners_for_deployment(
        sqlite_db,
        "parent-dep",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )

    assert runners is not None
    assert set(runners) == {
        "subgraph:child-dep:1:child-agent",
        "subgraph:grand-dep:2:grand-agent",
    }
    assert runners["subgraph:grand-dep:2:grand-agent"].config.input_model is RunInputPayload


def test_cli_parser_has_expected_subcommands():
    from zeroth.service.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["seed-demo", "--deployment-ref", "x", "--model", "m"])
    assert args.deployment_ref == "x"
    assert args.model == "m"
    args = parser.parse_args(["serve", "--port", "9000"])
    assert args.port == 9000
