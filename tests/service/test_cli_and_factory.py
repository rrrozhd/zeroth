"""Tests for the zeroth-core CLI seed path and the agent-runner factory."""

from __future__ import annotations

import pytest

from zeroth.contracts.registry import ContractRegistry
from zeroth.runtime.agents.factory import (
    AgentRunnerFactoryError,
    build_agent_runners,
)
from zeroth.runtime.agents.mcp import MCPServerConfig
from zeroth.service.bootstrap.factory import build_runners_for_deployment
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


def test_cli_parser_has_expected_subcommands():
    from zeroth.service.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["seed-demo", "--deployment-ref", "x", "--model", "m"])
    assert args.deployment_ref == "x"
    assert args.model == "m"
    args = parser.parse_args(["serve", "--port", "9000"])
    assert args.port == 9000
