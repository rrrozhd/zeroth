from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from pydantic import BaseModel

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutionSettings,
    Graph,
    SubgraphNode,
    SubgraphNodeData,
)
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.contracts.mappings.models import ConstantMappingOperation, EdgeMapping
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError
from zeroth.runtime.parallel.models import JoinConfig
from zeroth.runtime.runs import Run, RunStatus
from zeroth.runtime.subgraphs.executor import SubgraphExecutor


class Payload(BaseModel):
    value: int


class MemoryTokenStore:
    def __init__(self) -> None:
        self.snapshot: TokenEngineSnapshot | None = None
        self.history: list[TokenEngineSnapshot] = []

    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot | None:
        assert self.snapshot is None or self.snapshot.run_id == run_id
        return self.snapshot

    async def compare_and_swap_token_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        actual = None if self.snapshot is None else self.snapshot.revision
        if actual != expected_revision:
            raise TokenSnapshotConcurrencyError(
                run_id, expected_revision=expected_revision, actual_revision=actual
            )
        self.snapshot = snapshot
        self.history.append(snapshot)
        return snapshot


class RunOnlyRepository:
    """Deliberately exposes the Run API but not the token snapshot protocol."""

    def __init__(self, inner: RunRepository) -> None:
        self.inner = inner

    def __getattr__(self, name: str):
        if name in {"get_token_snapshot", "compare_and_swap_token_snapshot"}:
            raise AttributeError(name)
        return getattr(self.inner, name)


def _node(node_id: str) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref="token-adapter:v1",
        agent=AgentNodeData(instruction=node_id, model_provider=f"provider://{node_id}"),
    )


def _runner() -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name="token-adapter",
            instruction="test",
            model_name="governai:test",
            input_model=Payload,
            output_model=Payload,
        ),
        CallableProviderAdapter(
            lambda request: ProviderResponse(
                content={"value": request.metadata["input_payload"]["value"] + 1}
            )
        ),
    )


async def test_flag_on_linear_run_uses_injected_snapshot_store(sqlite_db) -> None:
    graph = Graph(
        graph_id="token-adapter",
        name="token-adapter",
        entry_step="A",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node("A"), _node("B")],
        edges=[Edge(edge_id="A-B", source_node_id="A", target_node_id="B")],
    )
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunOnlyRepository(RunRepository(sqlite_db)),
        agent_runners={"A": _runner(), "B": _runner()},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert [entry.node_id for entry in run.execution_history] == ["A", "B"]
    assert run.final_output == {"value": 3}
    assert run.pending_node_ids == []
    assert run.metadata["node_payloads"] == {}
    assert run.metadata["node_tags"] == {}
    assert store.snapshot is not None
    assert store.snapshot.state is TokenEngineSnapshotState.COMPLETED
    assert store.snapshot.queue == ()
    assert store.snapshot.in_flight_dispatches == ()


async def test_flag_on_requires_durable_snapshot_store(sqlite_db) -> None:
    graph = Graph(
        graph_id="token-adapter",
        name="token-adapter",
        entry_step="A",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node("A")],
        edges=[],
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=RunOnlyRepository(RunRepository(sqlite_db)),
        agent_runners={"A": _runner()},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    try:
        await orchestrator.run_graph(graph, {"value": 1})
    except RuntimeError as exc:
        assert "TokenSnapshotStore" in str(exc)
    else:  # pragma: no cover - assertion spelling
        raise AssertionError("flag-on execution accepted no token snapshot store")


async def test_flag_on_graph_fanout_creates_one_durable_child_per_edge(sqlite_db) -> None:
    graph = Graph(
        graph_id="token-adapter",
        name="token-adapter",
        entry_step="A",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node("A"), _node("B"), _node("C")],
        edges=[
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
        ],
    )
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in ("A", "B", "C")},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert [entry.node_id for entry in run.execution_history] == ["A", "B", "C"]
    assert store.snapshot is not None
    assert store.snapshot.state is TokenEngineSnapshotState.COMPLETED


async def test_flag_on_diamond_closes_structured_join_once(sqlite_db) -> None:
    join = _node("J")
    join.join_config = JoinConfig(merge_strategy="merge")
    graph = Graph(
        graph_id="token-adapter",
        name="token-adapter",
        entry_step="A",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node("A"), _node("L"), _node("R"), join],
        edges=[
            Edge(edge_id="A-L", source_node_id="A", target_node_id="L"),
            Edge(edge_id="A-R", source_node_id="A", target_node_id="R"),
            Edge(edge_id="L-J", source_node_id="L", target_node_id="J"),
            Edge(edge_id="R-J", source_node_id="R", target_node_id="J"),
        ],
    )
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in ("A", "L", "R", "J")},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert [entry.node_id for entry in run.execution_history] == ["A", "L", "R", "J"]
    assert sum(entry.node_id == "J" for entry in run.execution_history) == 1


async def test_flag_on_loop_persists_iteration_ownership(sqlite_db) -> None:
    graph = Graph(
        graph_id="token-loop-adapter",
        name="token-loop-adapter",
        entry_step="H",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node("H"), _node("B"), _node("OUT")],
        edges=[
            Edge(edge_id="H-B", source_node_id="H", target_node_id="B"),
            Edge(
                edge_id="B-H",
                source_node_id="B",
                target_node_id="H",
                condition=Condition(expression="payload.value < 4", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="B-OUT",
                source_node_id="B",
                target_node_id="OUT",
                condition=Condition(expression="payload.value >= 4"),
            ),
        ],
    )
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in ("H", "B", "OUT")},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert run.final_output == {"value": 5}
    assert any(snapshot.loops for snapshot in store.history)
    assert any(
        loop.lifecycle_state.value == "completed"
        for snapshot in store.history
        for loop in snapshot.loops
    )


async def test_flag_on_loop_header_materializes_body_and_distinct_exit_outcomes(
    sqlite_db,
) -> None:
    graph = Graph(
        graph_id="token-loop-header-boundaries",
        name="token-loop-header-boundaries",
        entry_step="H",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node(node_id) for node_id in ("H", "B", "X", "Y")],
        edges=[
            Edge(edge_id="H-B", source_node_id="H", target_node_id="B"),
            Edge(
                edge_id="B-H",
                source_node_id="B",
                target_node_id="H",
                condition=Condition(expression="payload.value < 0", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="H-X",
                source_node_id="H",
                target_node_id="X",
                mapping=EdgeMapping(
                    operations=[ConstantMappingOperation(target_path="value", value=10)]
                ),
            ),
            Edge(
                edge_id="H-Y",
                source_node_id="H",
                target_node_id="Y",
                mapping=EdgeMapping(
                    operations=[ConstantMappingOperation(target_path="value", value=20)]
                ),
            ),
        ],
    )
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in ("H", "B", "X", "Y")},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert [entry.node_id for entry in run.execution_history] == ["H", "B", "X", "Y"]
    completed = next(
        loop
        for snapshot in reversed(store.history)
        for loop in snapshot.loops
        if loop.lifecycle_state.value == "completed"
    )
    records = tuple(record for exit_state in completed.exits for record in exit_state.records)
    assert tuple(record.exit_edge_id for record in records) == ("H-X", "H-Y")
    assert tuple(record.delivery.payload for record in records if record.delivery is not None) == (
        {"value": 10},
        {"value": 20},
    )
    assert (
        len(
            {record.delivery.model_dump_json() for record in records if record.delivery is not None}
        )
        == 2
    )
    assert (
        TokenEngineSnapshot.model_validate_json(store.history[-2].model_dump_json())
        == store.history[-2]
    )


async def test_flag_on_loop_member_materializes_back_edge_and_exit_before_replay(
    sqlite_db,
) -> None:
    graph = Graph(
        graph_id="token-loop-member-boundaries",
        name="token-loop-member-boundaries",
        entry_step="H",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node(node_id) for node_id in ("H", "B", "X")],
        edges=[
            Edge(edge_id="H-B", source_node_id="H", target_node_id="B"),
            Edge(
                edge_id="B-H",
                source_node_id="B",
                target_node_id="H",
                condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="B-X",
                source_node_id="B",
                target_node_id="X",
                condition=Condition(expression="payload.value == 2"),
            ),
        ],
    )
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in ("H", "B", "X")},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert [entry.node_id for entry in run.execution_history] == ["H", "B", "H", "B", "X"]
    loop_states = [loop for snapshot in store.history for loop in snapshot.loops]
    exit_record = next(
        record for loop in loop_states for exit in loop.exits for record in exit.records
    )
    assert exit_record.exit_edge_id == "B-X"
    assert exit_record.delivery is not None
    assert exit_record.delivery.payload == {"value": 2}
    assert any(frame.continuation_deliveries for loop in loop_states for frame in loop.frames)


async def test_flag_on_nested_loop_boundary_settles_inner_before_outer_owner(
    sqlite_db,
) -> None:
    graph = Graph(
        graph_id="token-nested-loop-boundaries",
        name="token-nested-loop-boundaries",
        entry_step="O",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node(node_id) for node_id in ("O", "I", "B", "OB", "X")],
        edges=[
            Edge(edge_id="O-I", source_node_id="O", target_node_id="I"),
            Edge(edge_id="I-B", source_node_id="I", target_node_id="B"),
            Edge(
                edge_id="B-I",
                source_node_id="B",
                target_node_id="I",
                condition=Condition(expression="payload.value < 4", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="B-OB",
                source_node_id="B",
                target_node_id="OB",
                condition=Condition(expression="payload.value == 3"),
            ),
            Edge(
                edge_id="OB-O",
                source_node_id="OB",
                target_node_id="O",
                condition=Condition(expression="payload.value < 4", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="OB-X",
                source_node_id="OB",
                target_node_id="X",
                condition=Condition(expression="payload.value >= 4"),
            ),
        ],
    )
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in ("O", "I", "B", "OB", "X")},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert [entry.node_id for entry in run.execution_history] == [
        "O",
        "I",
        "B",
        "I",
        "B",
        "OB",
        "X",
    ]
    owner_snapshot = next(
        snapshot
        for snapshot in store.history
        if any(token.current_node_id == "OB" for token in snapshot.queue)
    )
    outer_owner = next(token for token in owner_snapshot.queue if token.current_node_id == "OB")
    assert tuple(item.loop_header_node_id for item in outer_owner.iteration_memberships) == ("O",)
    completed_order = [
        loop.loop_header_node_id
        for snapshot in store.history
        for loop in snapshot.loops
        if loop.lifecycle_state.value == "completed"
    ]
    assert completed_order.index("I") < completed_order.index("O")


async def test_flag_on_routes_subgraph_node_through_runtime_executor(sqlite_db) -> None:
    node = SubgraphNode(
        node_id="S",
        graph_version_ref="token-subgraph:v1",
        subgraph=SubgraphNodeData(graph_ref="child"),
    )
    graph = Graph(
        graph_id="token-subgraph",
        name="token-subgraph",
        entry_step="S",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[node],
        edges=[],
    )
    child = Run(
        graph_version_ref="child:v1",
        deployment_ref="child",
        status=RunStatus.COMPLETED,
        final_output={"value": 42},
    )
    executor = MagicMock(spec=SubgraphExecutor)
    executor.execute = AsyncMock(return_value=child)
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        subgraph_executor=executor,
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    assert run.final_output == {"value": 42}
    assert [entry.node_id for entry in run.execution_history] == ["S"]
    executor.execute.assert_awaited_once()


async def test_flag_on_resumes_paused_subgraph_without_creating_a_second_child(sqlite_db) -> None:
    node = SubgraphNode(
        node_id="S",
        graph_version_ref="token-subgraph:v1",
        subgraph=SubgraphNodeData(graph_ref="child"),
    )
    graph = Graph(
        graph_id="token-subgraph-resume",
        name="token-subgraph-resume",
        entry_step="S",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[node],
        edges=[],
    )
    child = Run(
        run_id="child-paused",
        graph_version_ref="child:v1",
        deployment_ref="child",
        status=RunStatus.WAITING_APPROVAL,
    )
    resumed_child = child.model_copy(
        update={"status": RunStatus.COMPLETED, "final_output": {"value": 84}}
    )
    executor = MagicMock(spec=SubgraphExecutor)
    executor.execute = AsyncMock(return_value=child)
    executor.resume = AsyncMock(return_value=resumed_child)
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        subgraph_executor=executor,
    ).use_token_snapshot_store(store)

    paused = await orchestrator.run_graph(graph, {"value": 1})
    resumed = await orchestrator.resume_graph(graph, paused.run_id)

    assert paused.status is RunStatus.WAITING_APPROVAL
    assert resumed.status is RunStatus.COMPLETED
    assert resumed.final_output == {"value": 84}
    executor.execute.assert_awaited_once()
    executor.resume.assert_awaited_once()
