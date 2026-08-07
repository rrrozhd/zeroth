"""Real-runtime integration for the independent token trace oracle."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest
from pydantic import BaseModel

from tests.orchestrator.test_token_engine_model import (
    Dispatch,
    EdgeResolution,
    EdgeSpec,
    TerminalState,
    Trace,
    TraceViolationError,
    assert_disabled_equals_removed,
    assert_trace_contract,
    payload_fingerprint,
)
from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.orchestration import token_scope as _ts
from zeroth.runtime.parallel.models import JoinConfig
from zeroth.runtime.runs import Run, RunStatus

pytestmark = pytest.mark.asyncio

EMPTY: _ts.TokenTag = ()
H0: _ts.TokenTag = (("H", 0),)
H1: _ts.TokenTag = (("H", 1),)


class Bag(BaseModel):
    value: int = 0
    left: int = 0
    right: int = 0


class LeftOutput(BaseModel):
    left: int


class RightOutput(BaseModel):
    right: int


def _agent(node_id: str, *, join: bool = False) -> AgentNode:
    node = AgentNode(
        node_id=node_id,
        graph_version_ref="trace:v1",
        agent=AgentNodeData(instruction="test", model_provider=f"provider://{node_id}"),
    )
    if join:
        node.join_config = JoinConfig(merge_strategy="merge")
    return node


def _runner(handler, output_model: type[BaseModel] = Bag) -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name="trace-agent",
            instruction="test",
            model_name="governai:test",
            input_model=Bag,
            output_model=output_model,
        ),
        CallableProviderAdapter(handler),
    )


def _emit(**fields: int):
    return lambda _req: ProviderResponse(content=fields)


def _echo(req):
    return ProviderResponse(content=dict(req.metadata["input_payload"]))


def _graph(nodes: list[AgentNode], edges: list[Edge], *, entry: str = "A") -> Graph:
    return Graph(
        graph_id="runtime-trace",
        name="runtime-trace",
        entry_step=entry,
        execution_settings=ExecutionSettings(
            max_total_steps=40,
            max_visits_per_node=10,
            sequential_join_enabled=True,
        ),
        nodes=nodes,
        edges=edges,
    )


class TracingOrchestrator(RuntimeOrchestrator):
    """Test-only observer for real token-engine resolution and dispatch seams."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.trace_resolutions: list[EdgeResolution] = []
        self.trace_dispatches: list[Dispatch] = []
        self._delivered: dict[tuple[str, _ts.TokenTag], list[str]] = defaultdict(list)

    def _record_forward_resolution(
        self,
        run: Run,
        target: str,
        edge_id: str,
        delivered: bool,
        payload: dict[str, Any] | None,
        tag: _ts.TokenTag,
    ) -> None:
        super()._record_forward_resolution(run, target, edge_id, delivered, payload, tag)
        fingerprint = payload_fingerprint(edge_id, payload) if delivered else None
        event = EdgeResolution(edge_id, tag, delivered, fingerprint)
        self.trace_resolutions.append(event)
        if fingerprint is not None:
            self._delivered[(target, tag)].append(fingerprint)

    def _stash_join_payload(
        self, run: Run, node_id: str, payload: dict[str, Any], tag: _ts.TokenTag
    ) -> None:
        super()._stash_join_payload(run, node_id, payload, tag)
        fingerprints = self._delivered.get((node_id, tag))
        if fingerprints:
            self.trace_dispatches.append(
                Dispatch(
                    node_id,
                    tag,
                    tuple(fingerprints),
                    payload_fingerprint(node_id, payload),
                )
            )

    def trace(
        self,
        graph: Graph,
        run: Run,
        expected_activations: tuple[tuple[str, _ts.TokenTag], ...],
        expected_dispatch_edges: tuple[tuple[str, _ts.TokenTag, tuple[str, ...]], ...],
        expected_dispatch_payloads: tuple[tuple[str, _ts.TokenTag, str], ...],
    ) -> Trace:
        def decode_tag(raw: Any) -> _ts.TokenTag:
            return tuple((str(header), int(iteration)) for header, iteration in (raw or ()))

        staged_payloads = tuple(
            sorted(
                (node, payload_fingerprint(node, payload))
                for node, payload in run.metadata.get("node_payloads", {}).items()
            )
        )
        staged_tags = tuple(
            sorted(
                (node, decode_tag(raw)) for node, raw in run.metadata.get("node_tags", {}).items()
            )
        )
        join_buckets = tuple(
            sorted(
                (node, decode_tag(entry.get("tag")))
                for node, node_state in run.metadata.get("join_state", {}).items()
                for entry in node_state.values()
            )
        )
        join_state_nodes = tuple(sorted(run.metadata.get("join_state", {})))
        back_edge_ids = self._back_edge_ids(graph)
        return Trace(
            edges=tuple(
                EdgeSpec(
                    edge.edge_id,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.enabled,
                )
                for edge in graph.edges
                if edge.kind != "tool" and edge.edge_id not in back_edge_ids
            ),
            resolutions=tuple(self.trace_resolutions),
            dispatches=tuple(self.trace_dispatches),
            expected_activations=expected_activations,
            terminal=TerminalState(
                pending_nodes=tuple(run.pending_node_ids),
                staged_payloads=staged_payloads,
                staged_tags=staged_tags,
                join_buckets=join_buckets,
                join_state_nodes=join_state_nodes,
            ),
            expected_dispatch_edges=expected_dispatch_edges,
            expected_dispatch_payloads=expected_dispatch_payloads,
        )


class CorruptingTracingOrchestrator(TracingOrchestrator):
    """Seeded fault proving actual staged dispatch payloads are observed."""

    def _merge_join_payloads(
        self, graph: Graph, node_id: str, payloads: list[dict[str, Any]]
    ) -> dict[str, Any]:
        merged = super()._merge_join_payloads(graph, node_id, payloads)
        if node_id == "J":
            return {"value": 999, "left": 0, "right": 0}
        return merged


def _orchestrator(runners: dict[str, AgentRunner], sqlite_db) -> TracingOrchestrator:
    return TracingOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners=runners,
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )


def _actual(node: str, **payload: int) -> str:
    return payload_fingerprint(node, payload)


async def test_real_runtime_trace_normal_dag_join(sqlite_db) -> None:
    graph = _graph(
        [_agent("A"), _agent("L"), _agent("R"), _agent("J", join=True)],
        [
            Edge(edge_id="A-L", source_node_id="A", target_node_id="L"),
            Edge(edge_id="A-R", source_node_id="A", target_node_id="R"),
            Edge(edge_id="L-J", source_node_id="L", target_node_id="J"),
            Edge(edge_id="R-J", source_node_id="R", target_node_id="J"),
        ],
    )
    orch = _orchestrator(
        {
            "A": _runner(_emit(value=1)),
            "L": _runner(_emit(left=10), LeftOutput),
            "R": _runner(_emit(right=20), RightOutput),
            "J": _runner(_echo),
        },
        sqlite_db,
    )

    run = await orch.run_graph(graph, {"value": 1})
    trace = orch.trace(
        graph,
        run,
        (("L", EMPTY), ("R", EMPTY), ("J", EMPTY)),
        (
            ("L", EMPTY, ("A-L",)),
            ("R", EMPTY, ("A-R",)),
            ("J", EMPTY, ("L-J", "R-J")),
        ),
        (
            ("L", EMPTY, _actual("L", value=1, left=0, right=0)),
            ("R", EMPTY, _actual("R", value=1, left=0, right=0)),
            ("J", EMPTY, _actual("J", left=10, right=20)),
        ),
    )

    assert run.status is RunStatus.COMPLETED
    assert_trace_contract(trace)
    join_dispatch = next(event for event in trace.dispatches if event.node == "J")
    assert {fp.split(":", 1)[0] for fp in join_dispatch.payload_fingerprints} == {
        "L-J",
        "R-J",
    }
    assert run.metadata["last_output"] == {"value": 0, "left": 10, "right": 20}


async def test_real_runtime_trace_detects_corrupted_join_payload(sqlite_db) -> None:
    graph = _graph(
        [_agent("A"), _agent("L"), _agent("R"), _agent("J", join=True)],
        [
            Edge(edge_id="A-L", source_node_id="A", target_node_id="L"),
            Edge(edge_id="A-R", source_node_id="A", target_node_id="R"),
            Edge(edge_id="L-J", source_node_id="L", target_node_id="J"),
            Edge(edge_id="R-J", source_node_id="R", target_node_id="J"),
        ],
    )
    orch = CorruptingTracingOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={
            "A": _runner(_emit(value=1)),
            "L": _runner(_emit(left=10), LeftOutput),
            "R": _runner(_emit(right=20), RightOutput),
            "J": _runner(_echo),
        },
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {"value": 1})
    trace = orch.trace(
        graph,
        run,
        (("L", EMPTY), ("R", EMPTY), ("J", EMPTY)),
        (
            ("L", EMPTY, ("A-L",)),
            ("R", EMPTY, ("A-R",)),
            ("J", EMPTY, ("L-J", "R-J")),
        ),
        (
            ("L", EMPTY, _actual("L", value=1, left=0, right=0)),
            ("R", EMPTY, _actual("R", value=1, left=0, right=0)),
            ("J", EMPTY, _actual("J", left=10, right=20)),
        ),
    )

    with pytest.raises(TraceViolationError, match="actual dispatch payload"):
        assert_trace_contract(trace)


async def test_real_runtime_trace_conditional_suppressed_parent(sqlite_db) -> None:
    graph = _graph(
        [_agent("A"), _agent("LOW"), _agent("HIGH"), _agent("J")],
        [
            Edge(
                edge_id="A-LOW",
                source_node_id="A",
                target_node_id="LOW",
                condition=Condition(expression="payload.value < 5"),
            ),
            Edge(
                edge_id="A-HIGH",
                source_node_id="A",
                target_node_id="HIGH",
                condition=Condition(expression="payload.value >= 5"),
            ),
            Edge(edge_id="LOW-J", source_node_id="LOW", target_node_id="J"),
            Edge(edge_id="HIGH-J", source_node_id="HIGH", target_node_id="J"),
        ],
    )
    orch = _orchestrator({node: _runner(_echo) for node in ("A", "LOW", "HIGH", "J")}, sqlite_db)

    run = await orch.run_graph(graph, {"value": 1})
    trace = orch.trace(
        graph,
        run,
        (("LOW", EMPTY), ("HIGH", EMPTY), ("J", EMPTY)),
        (("LOW", EMPTY, ("A-LOW",)), ("J", EMPTY, ("LOW-J",))),
        (
            ("LOW", EMPTY, _actual("LOW", value=1, left=0, right=0)),
            ("J", EMPTY, _actual("J", value=1, left=0, right=0)),
        ),
    )

    assert_trace_contract(trace)
    suppressed = next(event for event in trace.resolutions if event.edge_id == "A-HIGH")
    assert not suppressed.delivered
    assert suppressed.payload_fingerprint is None


async def test_real_runtime_disabled_edge_equals_removal(sqlite_db) -> None:
    def build(include_disabled: bool) -> Graph:
        edges = [Edge(edge_id="A-C", source_node_id="A", target_node_id="C")]
        if include_disabled:
            edges.insert(
                0,
                Edge(
                    edge_id="A-B",
                    source_node_id="A",
                    target_node_id="B",
                    enabled=False,
                ),
            )
        return _graph([_agent("A"), _agent("B"), _agent("C")], edges)

    async def execute(graph: Graph, expected, expected_edges, expected_payloads) -> Trace:
        orch = _orchestrator({node: _runner(_echo) for node in ("A", "B", "C")}, sqlite_db)
        run = await orch.run_graph(graph, {"value": 1})
        return orch.trace(graph, run, expected, expected_edges, expected_payloads)

    disabled = await execute(
        build(True),
        (("B", EMPTY), ("C", EMPTY)),
        (("C", EMPTY, ("A-C",)),),
        (("C", EMPTY, _actual("C", value=1, left=0, right=0)),),
    )
    removed = await execute(
        build(False),
        (("C", EMPTY),),
        (("C", EMPTY, ("A-C",)),),
        (("C", EMPTY, _actual("C", value=1, left=0, right=0)),),
    )

    assert_trace_contract(disabled)
    assert_trace_contract(removed)
    assert_disabled_equals_removed(disabled, removed)


async def test_runtime_terminal_capture_rejects_empty_raw_join_node_state(sqlite_db) -> None:
    graph = _graph(
        [_agent("A"), _agent("C")],
        [Edge(edge_id="A-C", source_node_id="A", target_node_id="C")],
    )
    orch = _orchestrator({"A": _runner(_echo), "C": _runner(_echo)}, sqlite_db)
    run = await orch.run_graph(graph, {"value": 1})
    run.metadata["join_state"] = {"J": {}}
    trace = orch.trace(
        graph,
        run,
        (("C", EMPTY),),
        (("C", EMPTY, ("A-C",)),),
        (("C", EMPTY, _actual("C", value=1, left=0, right=0)),),
    )

    with pytest.raises(TraceViolationError, match="join state nodes"):
        assert_trace_contract(trace)


async def test_real_runtime_trace_loop_exit_feeds_out_of_loop_join(sqlite_db) -> None:
    graph = _graph(
        [
            _agent("S"),
            _agent("P"),
            _agent("H"),
            _agent("W"),
            _agent("J", join=True),
            _agent("END"),
        ],
        [
            Edge(edge_id="S-P", source_node_id="S", target_node_id="P"),
            Edge(edge_id="S-H", source_node_id="S", target_node_id="H"),
            Edge(edge_id="P-J", source_node_id="P", target_node_id="J"),
            Edge(edge_id="H-W", source_node_id="H", target_node_id="W"),
            Edge(
                edge_id="W-H",
                source_node_id="W",
                target_node_id="H",
                condition=Condition(expression="payload.value < 2", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="W-J",
                source_node_id="W",
                target_node_id="J",
                condition=Condition(expression="payload.value >= 2"),
            ),
            Edge(edge_id="J-END", source_node_id="J", target_node_id="END"),
        ],
        entry="S",
    )

    def bump(req):
        return ProviderResponse(
            content={"value": req.metadata["input_payload"].get("value", 0) + 1}
        )

    orch = _orchestrator(
        {
            "S": _runner(_echo),
            "P": _runner(_echo),
            "H": _runner(_echo),
            "W": _runner(bump),
            "J": _runner(_echo),
            "END": _runner(_echo),
        },
        sqlite_db,
    )

    run = await orch.run_graph(graph, {"value": 0})
    trace = orch.trace(
        graph,
        run,
        (
            ("P", EMPTY),
            ("H", H0),
            ("W", H0),
            ("W", H1),
            ("J", EMPTY),
            ("END", EMPTY),
        ),
        (
            ("P", EMPTY, ("S-P",)),
            ("H", H0, ("S-H",)),
            ("W", H0, ("H-W",)),
            ("W", H1, ("H-W",)),
            ("J", EMPTY, ("P-J", "W-J")),
            ("END", EMPTY, ("J-END",)),
        ),
        (
            ("P", EMPTY, _actual("P", value=0, left=0, right=0)),
            ("H", H0, _actual("H", value=0, left=0, right=0)),
            ("W", H0, _actual("W", value=0, left=0, right=0)),
            ("W", H1, _actual("W", value=1, left=0, right=0)),
            ("J", EMPTY, _actual("J", value=2, left=0, right=0)),
            ("END", EMPTY, _actual("END", value=2, left=0, right=0)),
        ),
    )

    assert run.status is RunStatus.COMPLETED
    assert_trace_contract(trace)
    exit_event = next(event for event in trace.resolutions if event.edge_id == "W-J")
    assert exit_event.delivered and exit_event.tag == EMPTY
    # Back-edge re-entry is intentionally outside this forward-join oracle:
    # runtime dispatches H1 directly without recording a forward resolution or
    # staging it through `_stash_join_payload`. Its next H-W forward resolution
    # proves the re-entered header ran at H1.
    assert "W-H" not in {edge.edge_id for edge in trace.edges}
    assert "W-H" not in {event.edge_id for event in trace.resolutions}
    assert ("H", H1) not in {(event.node, event.tag) for event in trace.dispatches}
    assert ("W", H1) in {(event.node, event.tag) for event in trace.dispatches}
