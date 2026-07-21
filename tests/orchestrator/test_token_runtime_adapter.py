from __future__ import annotations

from pydantic import BaseModel

from zeroth.contracts.graph import AgentNode, AgentNodeData, Edge, ExecutionSettings, Graph
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot, TokenEngineSnapshotState
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.runtime.orchestration.token_snapshot_store import TokenSnapshotConcurrencyError


class Payload(BaseModel):
    value: int


class MemoryTokenStore:
    def __init__(self) -> None:
        self.snapshot: TokenEngineSnapshot | None = None

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
    assert "node_payloads" not in run.metadata
    assert "node_tags" not in run.metadata
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
