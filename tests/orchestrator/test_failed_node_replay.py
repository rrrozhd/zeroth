"""Durable replay of a node that failed after its dispatch was staged."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.contracts.graph import AgentNode, AgentNodeData, Edge, ExecutionSettings, Graph
from zeroth.core.orchestrator import OrchestratorError, RuntimeOrchestrator
from zeroth.runtime.parallel.errors import ParallelExecutionError
from zeroth.runtime.parallel.models import JoinConfig, ParallelConfig
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.runs import RunStatus

pytestmark = pytest.mark.asyncio


class Bag(BaseModel):
    value: int = 0
    left: int = 0
    right: int = 0
    done: bool = False


def _node(node_id: str, *, join: bool = False) -> AgentNode:
    node = AgentNode(
        node_id=node_id,
        graph_version_ref="failed-replay:v1",
        agent=AgentNodeData(instruction="test", model_provider=f"provider://{node_id}"),
    )
    if join:
        node.join_config = JoinConfig(merge_strategy="merge")
    return node


def _runner(handler) -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name="agent",
            instruction="test",
            model_name="governai:test",
            input_model=Bag,
            output_model=Bag,
        ),
        CallableProviderAdapter(handler),
    )


class RecordingOrchestrator(RuntimeOrchestrator):
    dispatch_records: list[dict[str, Any]]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dispatch_records = []

    async def _dispatch_node(self, node, run, input_payload, graph):
        if node.node_id == "target":
            self.dispatch_records.append(
                {
                    "input": dict(input_payload),
                    "in_flight": dict(run.metadata.get("in_flight_dispatch") or {}),
                }
            )
        return await super()._dispatch_node(node, run, input_payload, graph)


def _orchestrator(sqlite_db, handlers: dict[str, Any]) -> RecordingOrchestrator:
    return RecordingOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner(handler) for node_id, handler in handlers.items()},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )


def _linear_graph() -> Graph:
    return Graph(
        graph_id="failed-replay-off",
        name="failed-replay-off",
        entry_step="source",
        execution_settings=ExecutionSettings(sequential_join_enabled=False),
        nodes=[_node("source"), _node("target")],
        edges=[Edge(edge_id="source-target", source_node_id="source", target_node_id="target")],
    )


def _join_graph() -> Graph:
    return Graph(
        graph_id="failed-replay-on",
        name="failed-replay-on",
        entry_step="source",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node("source"), _node("left"), _node("right"), _node("target", join=True)],
        edges=[
            Edge(edge_id="source-left", source_node_id="source", target_node_id="left"),
            Edge(edge_id="source-right", source_node_id="source", target_node_id="right"),
            Edge(edge_id="left-target", source_node_id="left", target_node_id="target"),
            Edge(edge_id="right-target", source_node_id="right", target_node_id="target"),
        ],
    )


@pytest.mark.parametrize(
    "token_engine",
    [pytest.param(False, marks=pytest.mark.legacy_engine), True],
    ids=["legacy", "token-join"],
)
async def test_failed_dispatch_replays_identical_payload_and_token_once(
    sqlite_db, token_engine: bool
) -> None:
    graph = _join_graph() if token_engine else _linear_graph()
    attempts = 0

    def target(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient dispatch failure")
        return ProviderResponse(content={**request.metadata["input_payload"], "done": True})

    handlers = {
        "source": lambda _req: ProviderResponse(content={"value": 7}),
        "target": target,
    }
    if token_engine:
        handlers.update(
            {
                "left": lambda req: ProviderResponse(
                    content={**req.metadata["input_payload"], "left": 11}
                ),
                "right": lambda req: ProviderResponse(
                    content={**req.metadata["input_payload"], "right": 13}
                ),
            }
        )
    orchestrator = _orchestrator(sqlite_db, handlers)

    failed = await orchestrator.run_graph(graph, {"value": 7})

    assert failed.status is RunStatus.FAILED
    record = failed.metadata["in_flight_dispatch"]
    expected = {"value": 7, "left": 0, "right": 13, "done": False} if token_engine else {
        "value": 7,
        "left": 0,
        "right": 0,
        "done": False,
    }
    assert record["node_id"] == "target"
    assert record["input_payload"] == expected
    assert record["token_tag"] == ([] if token_engine else None)
    assert failed.pending_node_ids == []
    assert "target" not in failed.metadata["node_payloads"]

    repo = RunRepository(sqlite_db)
    await repo.transition(failed.run_id, RunStatus.PENDING)
    await repo.transition(failed.run_id, RunStatus.RUNNING)
    resumed = await orchestrator.resume_graph(graph, failed.run_id)

    assert resumed.status is RunStatus.COMPLETED
    target_records = orchestrator.dispatch_records
    assert [item["input"] for item in target_records] == [expected, expected]
    assert [item["in_flight"]["token_tag"] for item in target_records] == [
        [] if token_engine else None,
        [] if token_engine else None,
    ]
    assert attempts == 2
    assert [entry.node_id for entry in resumed.execution_history].count("target") == 1
    assert resumed.final_output == {**expected, "done": True}
    assert resumed.pending_node_ids == []
    assert resumed.current_node_ids == []
    assert not resumed.metadata.get("node_payloads")
    assert not resumed.metadata.get("node_tags")
    assert not resumed.metadata.get("join_state")
    assert "in_flight_dispatch" not in resumed.metadata


async def test_replay_rejects_conflicting_staged_payload(sqlite_db) -> None:
    attempts = 0

    def target(_request):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("transient dispatch failure")

    graph = _linear_graph()
    orchestrator = _orchestrator(
        sqlite_db,
        {
            "source": lambda _req: ProviderResponse(content={"value": 7}),
            "target": target,
        },
    )
    failed = await orchestrator.run_graph(graph, {"value": 7})
    failed.metadata["node_payloads"]["target"] = {"value": 999}
    await orchestrator.run_repository.put(failed)
    await orchestrator.run_repository.transition(failed.run_id, RunStatus.PENDING)
    await orchestrator.run_repository.transition(failed.run_id, RunStatus.RUNNING)

    with pytest.raises(OrchestratorError, match="conflicting staged payload"):
        await orchestrator.resume_graph(graph, failed.run_id)

    assert attempts == 1


@pytest.mark.legacy_engine
async def test_failed_fan_out_does_not_create_ordinary_in_flight_record(sqlite_db) -> None:
    class FailingFanOutOrchestrator(RecordingOrchestrator):
        async def _execute_parallel_fan_out(self, *args, **kwargs):
            raise ParallelExecutionError("branch failed")

    source = _node("source")
    source.parallel_config = ParallelConfig(split_path="items")
    graph = Graph(
        graph_id="failed-fan-out",
        name="failed-fan-out",
        entry_step="source",
        execution_settings=ExecutionSettings(sequential_join_enabled=False),
        nodes=[source],
        edges=[],
    )
    orchestrator = FailingFanOutOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={
            "source": _runner(lambda _req: ProviderResponse(content={"value": 7}))
        },
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    failed = await orchestrator.run_graph(graph, {"value": 7})

    assert failed.status is RunStatus.FAILED
    assert failed.failure_state is not None
    assert failed.failure_state.reason == "parallel_execution_failed"
    assert "in_flight_dispatch" not in failed.metadata


async def test_replay_rejects_in_flight_marker_for_parallel_node(sqlite_db) -> None:
    attempts = 0

    def target(_request):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("transient dispatch failure")

    graph = _linear_graph()
    orchestrator = _orchestrator(
        sqlite_db,
        {
            "source": lambda _req: ProviderResponse(content={"value": 7}),
            "target": target,
        },
    )
    failed = await orchestrator.run_graph(graph, {"value": 7})
    target_node = next(node for node in graph.nodes if node.node_id == "target")
    target_node.parallel_config = ParallelConfig(split_path="items")
    await orchestrator.run_repository.transition(failed.run_id, RunStatus.PENDING)
    await orchestrator.run_repository.transition(failed.run_id, RunStatus.RUNNING)

    with pytest.raises(OrchestratorError, match="parallel fan-out node"):
        await orchestrator.resume_graph(graph, failed.run_id)

    assert attempts == 1
