from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutionSettings,
    Graph,
    JoinObligationOutcome,
    LoopLifecycleState,
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
from zeroth.runtime.orchestration.token_runtime_support import TokenRuntimeSupport
from zeroth.runtime.orchestration.token_scheduler import (
    FanOutBranch,
    claim_next_token,
    fan_out_dispatch,
    initialize_token_snapshot,
)
from zeroth.runtime.orchestration.token_lifecycle import request_cancellation, stop_snapshot
from zeroth.runtime.orchestration.token_scheduler import TokenSchedulerTransitionError
from zeroth.runtime.parallel.models import JoinConfig
from zeroth.runtime.graph_validation import GraphValidator
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


class ReloadingMemoryTokenStore(MemoryTokenStore):
    """Force every CAS/reload through the persisted JSON representation."""

    async def get_token_snapshot(self, run_id: str) -> TokenEngineSnapshot | None:
        snapshot = await super().get_token_snapshot(run_id)
        return (
            None
            if snapshot is None
            else TokenEngineSnapshot.model_validate_json(snapshot.model_dump_json())
        )

    async def compare_and_swap_token_snapshot(
        self,
        run_id: str,
        *,
        expected_revision: int | None,
        snapshot: TokenEngineSnapshot,
    ) -> TokenEngineSnapshot:
        restored = TokenEngineSnapshot.model_validate_json(snapshot.model_dump_json())
        committed = await super().compare_and_swap_token_snapshot(
            run_id,
            expected_revision=expected_revision,
            snapshot=restored,
        )
        return TokenEngineSnapshot.model_validate_json(committed.model_dump_json())


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


@pytest.mark.parametrize(
    ("upstream_condition", "downstream_condition", "expected_final"),
    [
        (None, None, {"value": 4}),
        (None, Condition(expression="payload.value < 0"), {"value": 3}),
        (Condition(expression="payload.value < 0"), None, {"value": 4}),
    ],
    ids=("both-delivered", "downstream-suppressed", "upstream-suppressed"),
)
async def test_default_on_nested_diamond_resolves_join_and_other_successor_once(
    sqlite_db, upstream_condition, downstream_condition, expected_final
) -> None:
    nodes = {node_id: _node(node_id) for node_id in ("A", "B", "C", "J", "T")}
    for node in nodes.values():
        node.input_contract_ref = "contract://input"
        node.output_contract_ref = "contract://output"
    nodes["J"].join_config = JoinConfig(merge_strategy="merge")
    nodes["T"].join_config = JoinConfig(merge_strategy="merge")
    graph = Graph(
        graph_id="token-nested-diamond",
        name="token-nested-diamond",
        entry_step="A",
        execution_settings=ExecutionSettings(),
        nodes=list(nodes.values()),
        edges=[
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            Edge(
                edge_id="B-J",
                source_node_id="B",
                target_node_id="J",
                condition=upstream_condition,
            ),
            Edge(edge_id="B-T", source_node_id="B", target_node_id="T"),
            Edge(edge_id="C-J", source_node_id="C", target_node_id="J"),
            Edge(
                edge_id="J-T",
                source_node_id="J",
                target_node_id="T",
                condition=downstream_condition,
            ),
        ],
    )
    report = await GraphValidator().validate(graph)
    assert report.is_valid, report.issues
    store = ReloadingMemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in nodes},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert run.status is RunStatus.COMPLETED, run.error
    assert [entry.node_id for entry in run.execution_history] == ["A", "B", "C", "J", "T"]
    assert sum(entry.node_id == "J" for entry in run.execution_history) == 1
    assert sum(entry.node_id == "T" for entry in run.execution_history) == 1
    assert run.final_output == expected_final
    deferred = next(snapshot for snapshot in store.history if snapshot.deferred_join_deliveries)
    assert TokenEngineSnapshot.model_validate_json(deferred.model_dump_json()) == deferred
    assert [
        delivery.delivery.model_dump(mode="json")["payload"]
        for delivery in deferred.deferred_join_deliveries
    ] == [{"value": 2}]
    persisted_delivery = deferred.deferred_join_deliveries[0]
    persisted_dispatch = deferred.in_flight_dispatches[0]
    assert (
        TokenRuntimeSupport._append_deferred_join_delivery(
            deferred,
            target_node_id=persisted_delivery.target_node_id,
            inbound_edge_id=persisted_delivery.inbound_edge_id,
            payload=persisted_delivery.delivery.model_dump(mode="json")["payload"],
            dispatch_id=persisted_dispatch.dispatch_id,
            attempt=persisted_dispatch.attempt,
            cancellation_generation=persisted_dispatch.cancellation_generation,
        )
        is deferred
    )
    queued_target = next(
        token
        for snapshot in store.history
        if not snapshot.deferred_join_deliveries
        for token in snapshot.queue
        if token.current_node_id == "T"
    )
    assert queued_target.causal_inbound_edge_id == "B-T"


def test_stale_completion_cannot_append_deferred_join_delivery_after_cancellation() -> None:
    root = initialize_token_snapshot(run_id="stale-deferred", root_node_id="A", payload={})
    parent = claim_next_token(root)
    forked = fan_out_dispatch(
        parent.snapshot,
        dispatch_id=parent.dispatch.dispatch_id,
        attempt=parent.dispatch.attempt,
        cancellation_generation=parent.dispatch.cancellation_generation,
        branches=(
            FanOutBranch(node_id="B", inbound_edge_id="A-B", payload={}),
            FanOutBranch(node_id="C", inbound_edge_id="A-C", payload={}),
        ),
    )
    stale = claim_next_token(forked)
    cancelling = request_cancellation(stale.snapshot)

    with pytest.raises(TokenSchedulerTransitionError):
        TokenRuntimeSupport._append_deferred_join_delivery(
            cancelling,
            target_node_id="T",
            inbound_edge_id="B-T",
            payload={"value": 1},
            dispatch_id=stale.dispatch.dispatch_id,
            attempt=stale.dispatch.attempt,
            cancellation_generation=stale.dispatch.cancellation_generation,
        )


async def test_default_on_nested_fanout_inner_join_preserves_outer_ownership(sqlite_db) -> None:
    node_ids = ("A", "X", "Y", "B", "C", "J", "T")
    nodes = {node_id: _node(node_id) for node_id in node_ids}
    for node in nodes.values():
        node.input_contract_ref = "contract://input"
        node.output_contract_ref = "contract://output"
    nodes["J"].join_config = JoinConfig(merge_strategy="merge")
    nodes["T"].join_config = JoinConfig(merge_strategy="merge")
    graph = Graph(
        graph_id="token-nested-fanout-inner-join",
        name="token-nested-fanout-inner-join",
        entry_step="A",
        execution_settings=ExecutionSettings(),
        nodes=list(nodes.values()),
        edges=[
            Edge(edge_id="A-X", source_node_id="A", target_node_id="X"),
            Edge(edge_id="A-Y", source_node_id="A", target_node_id="Y"),
            Edge(edge_id="X-B", source_node_id="X", target_node_id="B"),
            Edge(edge_id="X-C", source_node_id="X", target_node_id="C"),
            Edge(edge_id="B-J", source_node_id="B", target_node_id="J"),
            Edge(edge_id="B-T", source_node_id="B", target_node_id="T"),
            Edge(edge_id="C-J", source_node_id="C", target_node_id="J"),
            Edge(edge_id="J-T", source_node_id="J", target_node_id="T"),
        ],
    )
    report = await GraphValidator().validate(graph)
    assert report.is_valid, report.issues
    store = ReloadingMemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in node_ids},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert run.status is RunStatus.COMPLETED, run.error
    assert [entry.node_id for entry in run.execution_history] == [
        "A",
        "X",
        "B",
        "C",
        "Y",
        "J",
        "T",
    ]
    assert sum(entry.node_id == "J" for entry in run.execution_history) == 1
    assert sum(entry.node_id == "T" for entry in run.execution_history) == 1
    assert store.snapshot is not None
    assert store.snapshot.state is TokenEngineSnapshotState.COMPLETED


@pytest.mark.parametrize("outer_x_first", [True, False], ids=("inner-first", "sibling-first"))
@pytest.mark.parametrize("inner_c_first", [False, True], ids=("b-child-first", "c-child-first"))
async def test_nested_inner_and_outer_cohorts_reconverge_at_same_join(
    sqlite_db, outer_x_first, inner_c_first
) -> None:
    node_ids = ("A", "X", "Y", "B", "C", "J", "T")
    nodes = {node_id: _node(node_id) for node_id in node_ids}
    for node in nodes.values():
        node.input_contract_ref = "contract://input"
        node.output_contract_ref = "contract://output"
    nodes["J"].join_config = JoinConfig(merge_strategy="merge")
    graph = Graph(
        graph_id="token-nested-shared-join",
        name="token-nested-shared-join",
        entry_step="A",
        execution_settings=ExecutionSettings(),
        nodes=list(nodes.values()),
        edges=[
            *(
                [
                    Edge(edge_id="A-X", source_node_id="A", target_node_id="X"),
                    Edge(edge_id="A-Y", source_node_id="A", target_node_id="Y"),
                ]
                if outer_x_first
                else [
                    Edge(edge_id="A-Y", source_node_id="A", target_node_id="Y"),
                    Edge(edge_id="A-X", source_node_id="A", target_node_id="X"),
                ]
            ),
            *(
                [
                    Edge(edge_id="X-C", source_node_id="X", target_node_id="C"),
                    Edge(edge_id="X-B", source_node_id="X", target_node_id="B"),
                ]
                if inner_c_first
                else [
                    Edge(edge_id="X-B", source_node_id="X", target_node_id="B"),
                    Edge(edge_id="X-C", source_node_id="X", target_node_id="C"),
                ]
            ),
            Edge(edge_id="B-J", source_node_id="B", target_node_id="J"),
            Edge(edge_id="C-J", source_node_id="C", target_node_id="J"),
            Edge(edge_id="Y-J", source_node_id="Y", target_node_id="J"),
            Edge(edge_id="J-T", source_node_id="J", target_node_id="T"),
        ],
    )
    report = await GraphValidator().validate(graph)
    assert report.is_valid, report.issues
    store = ReloadingMemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in node_ids},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert run.status is RunStatus.COMPLETED, run.error
    inner_children = ["C", "B"] if inner_c_first else ["B", "C"]
    expected_middle = ["X", *inner_children, "Y"] if outer_x_first else ["Y", "X", *inner_children]
    assert [entry.node_id for entry in run.execution_history] == [
        "A",
        *expected_middle,
        "J",
        "T",
    ]
    assert sum(entry.node_id == "J" for entry in run.execution_history) == 1
    assert store.snapshot is not None
    assert store.snapshot.state is TokenEngineSnapshotState.COMPLETED


@pytest.mark.parametrize("outer_x_first", [True, False], ids=("inner-first", "sibling-first"))
async def test_nested_shared_join_does_not_reapply_representative_edge_mapping(
    sqlite_db, outer_x_first
) -> None:
    node_ids = ("A", "X", "Y", "B", "C", "J", "T")
    nodes = {node_id: _node(node_id) for node_id in node_ids}
    for node in nodes.values():
        node.input_contract_ref = "contract://input"
        node.output_contract_ref = "contract://output"
    nodes["J"].join_config = JoinConfig(merge_strategy="merge")
    graph = Graph(
        graph_id="token-nested-shared-join-mappings",
        name="token-nested-shared-join-mappings",
        entry_step="A",
        execution_settings=ExecutionSettings(),
        nodes=list(nodes.values()),
        edges=[
            *(
                [
                    Edge(edge_id="A-X", source_node_id="A", target_node_id="X"),
                    Edge(edge_id="A-Y", source_node_id="A", target_node_id="Y"),
                ]
                if outer_x_first
                else [
                    Edge(edge_id="A-Y", source_node_id="A", target_node_id="Y"),
                    Edge(edge_id="A-X", source_node_id="A", target_node_id="X"),
                ]
            ),
            Edge(edge_id="X-B", source_node_id="X", target_node_id="B"),
            Edge(edge_id="X-C", source_node_id="X", target_node_id="C"),
            Edge(
                edge_id="B-J",
                source_node_id="B",
                target_node_id="J",
                mapping=EdgeMapping(
                    operations=[ConstantMappingOperation(target_path="value", value=10)]
                ),
            ),
            Edge(
                edge_id="C-J",
                source_node_id="C",
                target_node_id="J",
                mapping=EdgeMapping(
                    operations=[ConstantMappingOperation(target_path="value", value=20)]
                ),
            ),
            Edge(
                edge_id="Y-J",
                source_node_id="Y",
                target_node_id="J",
                condition=Condition(expression="payload.value < 0"),
                mapping=EdgeMapping(
                    operations=[ConstantMappingOperation(target_path="value", value=30)]
                ),
            ),
            Edge(edge_id="J-T", source_node_id="J", target_node_id="T"),
        ],
    )
    report = await GraphValidator().validate(graph)
    assert report.is_valid, report.issues
    store = ReloadingMemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in node_ids},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert run.status is RunStatus.COMPLETED, run.error
    j_entry = next(entry for entry in run.execution_history if entry.node_id == "J")
    assert j_entry.input_snapshot == {"value": 20}
    assert run.final_output == {"value": 22}


@pytest.mark.parametrize("outer_x_first", [True, False], ids=("inner-first", "sibling-first"))
async def test_nested_shared_join_all_suppressed_inner_settles_outer_slot(
    sqlite_db, outer_x_first
) -> None:
    node_ids = ("A", "X", "Y", "B", "C", "J", "T")
    nodes = {node_id: _node(node_id) for node_id in node_ids}
    for node in nodes.values():
        node.input_contract_ref = "contract://input"
        node.output_contract_ref = "contract://output"
    nodes["J"].join_config = JoinConfig(merge_strategy="merge")
    graph = Graph(
        graph_id="token-nested-shared-join-suppressed",
        name="token-nested-shared-join-suppressed",
        entry_step="A",
        execution_settings=ExecutionSettings(),
        nodes=list(nodes.values()),
        edges=[
            *(
                [
                    Edge(edge_id="A-X", source_node_id="A", target_node_id="X"),
                    Edge(edge_id="A-Y", source_node_id="A", target_node_id="Y"),
                ]
                if outer_x_first
                else [
                    Edge(edge_id="A-Y", source_node_id="A", target_node_id="Y"),
                    Edge(edge_id="A-X", source_node_id="A", target_node_id="X"),
                ]
            ),
            Edge(edge_id="X-B", source_node_id="X", target_node_id="B"),
            Edge(edge_id="X-C", source_node_id="X", target_node_id="C"),
            Edge(
                edge_id="B-J",
                source_node_id="B",
                target_node_id="J",
                condition=Condition(expression="payload.value < 0"),
            ),
            Edge(
                edge_id="C-J",
                source_node_id="C",
                target_node_id="J",
                condition=Condition(expression="payload.value < 0"),
            ),
            Edge(edge_id="Y-J", source_node_id="Y", target_node_id="J"),
            Edge(edge_id="J-T", source_node_id="J", target_node_id="T"),
        ],
    )
    report = await GraphValidator().validate(graph)
    assert report.is_valid, report.issues
    store = ReloadingMemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in node_ids},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert run.status is RunStatus.COMPLETED, run.error
    expected_middle = ["X", "B", "C", "Y"] if outer_x_first else ["Y", "X", "B", "C"]
    assert [entry.node_id for entry in run.execution_history] == [
        "A",
        *expected_middle,
        "J",
        "T",
    ]
    assert run.final_output == {"value": 4}


async def test_cancellation_settles_delegated_outer_join_obligation(sqlite_db) -> None:
    class _Captured(BaseException):
        pass

    class _CancellingStore(ReloadingMemoryTokenStore):
        captured = False

        async def compare_and_swap_token_snapshot(self, run_id, *, expected_revision, snapshot):
            delegated = len(snapshot.forks) == 2 and any(
                obligation.outcome is None
                and next(
                    token
                    for token in snapshot.tokens
                    if token.token_id == obligation.source_token_id
                ).scheduling_state.value
                == "settled"
                for join in snapshot.joins
                for obligation in join.obligations
            )
            if not self.captured and delegated:
                actual = None if self.snapshot is None else self.snapshot.revision
                assert actual == expected_revision
                cancelled = request_cancellation(snapshot)
                self.snapshot = TokenEngineSnapshot.model_validate_json(cancelled.model_dump_json())
                self.history.append(self.snapshot)
                self.captured = True
                raise _Captured
            return await super().compare_and_swap_token_snapshot(
                run_id, expected_revision=expected_revision, snapshot=snapshot
            )

    nodes = {node_id: _node(node_id) for node_id in ("A", "X", "Y", "B", "C", "J")}
    for node in nodes.values():
        node.input_contract_ref = "contract://input"
        node.output_contract_ref = "contract://output"
    nodes["J"].join_config = JoinConfig(merge_strategy="merge")
    graph = Graph(
        graph_id="token-nested-shared-join-cancel",
        name="token-nested-shared-join-cancel",
        entry_step="A",
        execution_settings=ExecutionSettings(),
        nodes=list(nodes.values()),
        edges=[
            Edge(edge_id="A-Y", source_node_id="A", target_node_id="Y"),
            Edge(edge_id="A-X", source_node_id="A", target_node_id="X"),
            Edge(edge_id="X-B", source_node_id="X", target_node_id="B"),
            Edge(edge_id="X-C", source_node_id="X", target_node_id="C"),
            Edge(edge_id="B-J", source_node_id="B", target_node_id="J"),
            Edge(edge_id="C-J", source_node_id="C", target_node_id="J"),
            Edge(edge_id="Y-J", source_node_id="Y", target_node_id="J"),
        ],
    )
    store = _CancellingStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in nodes},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    with pytest.raises(_Captured):
        await orchestrator.run_graph(graph, {"value": 0})

    assert store.captured
    assert store.snapshot is not None
    assert store.snapshot.state is TokenEngineSnapshotState.CANCELLED
    assert all(join.lifecycle_state.value != "open" for join in store.snapshot.joins)


async def test_graceful_stop_drains_persisted_overlapping_join_frontier(sqlite_db) -> None:
    nodes = {node_id: _node(node_id) for node_id in ("A", "B", "C", "J", "T")}
    for node in nodes.values():
        node.input_contract_ref = "contract://input"
        node.output_contract_ref = "contract://output"
    nodes["J"].join_config = JoinConfig(merge_strategy="merge")
    nodes["T"].join_config = JoinConfig(merge_strategy="merge")
    graph = Graph(
        graph_id="token-graceful-overlap",
        name="token-graceful-overlap",
        entry_step="A",
        execution_settings=ExecutionSettings(),
        nodes=list(nodes.values()),
        edges=[
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            Edge(edge_id="B-J", source_node_id="B", target_node_id="J"),
            Edge(edge_id="B-T", source_node_id="B", target_node_id="T"),
            Edge(edge_id="C-J", source_node_id="C", target_node_id="J"),
            Edge(edge_id="J-T", source_node_id="J", target_node_id="T"),
        ],
    )
    repository = RunRepository(sqlite_db)

    class _GracefulStopStore(ReloadingMemoryTokenStore):
        stopping = False

        async def compare_and_swap_token_snapshot(self, run_id, *, expected_revision, snapshot):
            if not self.stopping and snapshot.deferred_join_deliveries:
                snapshot = stop_snapshot(snapshot)
                self.stopping = True
            committed = await super().compare_and_swap_token_snapshot(
                run_id, expected_revision=expected_revision, snapshot=snapshot
            )
            if self.stopping:
                persisted = await repository.get(run_id)
                if persisted is not None:
                    persisted.status = RunStatus.WAITING_INTERRUPT
                    persisted.touch()
                    await repository.put(persisted)
            return committed

    store = _GracefulStopStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={node_id: _runner() for node_id in nodes},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert store.stopping
    assert run.status is RunStatus.WAITING_INTERRUPT
    assert [entry.node_id for entry in run.execution_history] == ["A", "B", "C", "J", "T"]
    assert store.snapshot is not None
    assert store.snapshot.state is TokenEngineSnapshotState.STOPPED
    assert store.snapshot.queue == ()
    assert store.snapshot.deferred_join_deliveries == ()


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


async def test_flag_on_loop_member_keeps_internal_child_and_records_exit(sqlite_db) -> None:
    graph = Graph(
        graph_id="token-loop-mixed-internal-exit",
        name="token-loop-mixed-internal-exit",
        entry_step="H",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_node(node_id) for node_id in ("H", "B", "C", "X")],
        edges=[
            Edge(edge_id="H-B", source_node_id="H", target_node_id="B"),
            Edge(
                edge_id="B-C",
                source_node_id="B",
                target_node_id="C",
                condition=Condition(expression="payload.value < 3"),
            ),
            Edge(edge_id="B-X", source_node_id="B", target_node_id="X"),
            Edge(
                edge_id="C-H",
                source_node_id="C",
                target_node_id="H",
                condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True),
            ),
        ],
    )
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in ("H", "B", "C", "X")},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert [entry.node_id for entry in run.execution_history] == ["H", "B", "C", "X"]
    completed = next(
        loop
        for snapshot in reversed(store.history)
        for loop in snapshot.loops
        if loop.lifecycle_state is LoopLifecycleState.COMPLETED
    )
    exit_state = next(item for item in completed.exits if item.exit_edge_id == "B-X")
    assert len(exit_state.records) == 1
    assert exit_state.records[0].delivery.payload == {"value": 2}


async def test_flag_on_nested_inner_back_edge_and_outer_exit_waits_for_owner(
    sqlite_db,
) -> None:
    graph = Graph(
        graph_id="token-nested-back-and-outer-exit",
        name="token-nested-back-and-outer-exit",
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
                edge_id="B-X",
                source_node_id="B",
                target_node_id="X",
                condition=Condition(expression="payload.value == 3"),
            ),
            Edge(
                edge_id="B-OB",
                source_node_id="B",
                target_node_id="OB",
                condition=Condition(expression="payload.value >= 5"),
            ),
            Edge(
                edge_id="OB-O",
                source_node_id="OB",
                target_node_id="O",
                condition=Condition(expression="payload.value < 0", allow_cycle_traversal=True),
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

    assert run.status is RunStatus.COMPLETED
    assert [entry.node_id for entry in run.execution_history] == [
        "O",
        "I",
        "B",
        "I",
        "B",
        "OB",
        "X",
    ]
    completed_order = [
        loop.loop_header_node_id
        for snapshot in store.history
        for loop in snapshot.loops
        if loop.lifecycle_state is LoopLifecycleState.COMPLETED
    ]
    assert completed_order.index("I") < completed_order.index("O")


async def test_flag_on_multi_boundary_exit_hands_off_reserved_join_once(sqlite_db) -> None:
    join_node = _node("J")
    join_node.join_config = JoinConfig(merge_strategy="merge")
    graph = Graph(
        graph_id="token-loop-multi-boundary-join",
        name="token-loop-multi-boundary-join",
        entry_step="S",
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[
            *[_node(node_id) for node_id in ("S", "P", "H", "B")],
            join_node,
            _node("END"),
        ],
        edges=[
            Edge(edge_id="S-P", source_node_id="S", target_node_id="P"),
            Edge(edge_id="S-H", source_node_id="S", target_node_id="H"),
            Edge(edge_id="P-J", source_node_id="P", target_node_id="J"),
            Edge(edge_id="H-B", source_node_id="H", target_node_id="B"),
            Edge(
                edge_id="B-H",
                source_node_id="B",
                target_node_id="H",
                condition=Condition(expression="payload.value < 4", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="B-J",
                source_node_id="B",
                target_node_id="J",
                condition=Condition(expression="payload.value == 3"),
            ),
            Edge(edge_id="J-END", source_node_id="J", target_node_id="END"),
        ],
    )
    store = MemoryTokenStore()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={node_id: _runner() for node_id in ("S", "P", "H", "B", "J", "END")},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    ).use_token_snapshot_store(store)

    run = await orchestrator.run_graph(graph, {"value": 0})

    assert run.status is RunStatus.COMPLETED, run.error
    assert [entry.node_id for entry in run.execution_history].count("J") == 1
    assert [entry.node_id for entry in run.execution_history][-2:] == ["J", "END"]
    completed_loop = next(
        loop
        for snapshot in reversed(store.history)
        for loop in snapshot.loops
        if loop.loop_header_node_id == "H" and loop.lifecycle_state is LoopLifecycleState.COMPLETED
    )
    assert [record.delivery.payload for record in completed_loop.exits[0].records] == [{"value": 3}]
    completed_join = next(
        join
        for snapshot in reversed(store.history)
        for join in snapshot.joins
        if join.target_node_id == "J" and join.lifecycle_state.value == "closed"
    )
    assert all(obligation.outcome is not None for obligation in completed_join.obligations)
    loop_exit = next(
        obligation
        for obligation in completed_join.obligations
        if obligation.inbound_edge_id == "B-J"
    )
    assert loop_exit.outcome is JoinObligationOutcome.DELIVERED
    closed_reserved_revisions = {
        fork.updated_revision
        for snapshot in store.history
        for fork in snapshot.forks
        if fork.fork_id == loop_exit.fork_id and fork.lifecycle_state.value == "closed"
    }
    exit_settled_revision = completed_loop.exits[0].records[0].settled_revision
    assert exit_settled_revision not in closed_reserved_revisions


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
