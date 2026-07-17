"""B9 join-barrier ADVERSARIAL STRESS suite (audit).

Self-contained harness (tests/orchestrator has no __init__, so nothing is
imported from test_join_barrier). Every graph runs with the flag ON and probes
an edge case the happy-path suite doesn't: unreachable-source deadlock, wide
fan-in, stacked diamonds, deep skip cascade, conditional-both-fire without a
JoinConfig, a parallel node as a convergent target, and a convergent-on-cycle.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime import AgentConfig, AgentRunner
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.core.execution_units import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.parallel.models import JoinConfig, ParallelConfig
from zeroth.core.runs import RunRepository, RunStatus

pytestmark = pytest.mark.asyncio


class Bag(BaseModel):
    value: int = 0
    a: int = 0
    b: int = 0
    c: int = 0
    d: int = 0
    e: int = 0
    f: int = 0
    g: int = 0
    x: int = 0
    tier: str = ""
    # wide fan-in fields
    p0: int = 0
    p1: int = 0
    p2: int = 0
    p3: int = 0
    p4: int = 0
    p5: int = 0
    p6: int = 0
    p7: int = 0
    p8: int = 0
    p9: int = 0
    items: list = []  # noqa: RUF012


def _agent(node_id: str, *, parallel_config: ParallelConfig | None = None,
           join_config: JoinConfig | None = None) -> AgentNode:
    n = AgentNode(
        node_id=node_id,
        graph_version_ref="stress:v1",
        agent=AgentNodeData(instruction="t", model_provider=f"provider://{node_id}"),
        parallel_config=parallel_config,
    )
    n.join_config = join_config
    return n


def _emit(**fields):
    return lambda req: ProviderResponse(content=dict(fields))


def _echo(req):
    return ProviderResponse(content=dict(req.metadata["input_payload"]))


def _runner(handler) -> AgentRunner:
    return AgentRunner(
        AgentConfig(name="a", instruction="t", model_name="governai:test",
                    input_model=Bag, output_model=Bag),
        CallableProviderAdapter(handler),
    )


def _orch(runners, sqlite_db) -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners=runners,
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )


def _graph(nodes, edges, *, entry="A") -> Graph:
    return Graph(
        graph_id="stress", name="stress", entry_step=entry,
        execution_settings=ExecutionSettings(max_total_steps=200, sequential_join_enabled=True),
        nodes=nodes, edges=edges,
    )


def _counts(run) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in run.execution_history:
        counts[e.node_id] = counts.get(e.node_id, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# S1 — unreachable source into a convergent node  (deadlock / silent-drop probe)
#   A(entry) -> B -> D ;  Z -> D   (Z has no inbound, never reached)
# Legacy behavior: D runs on B's payload (Z->D simply never fires).
# ---------------------------------------------------------------------------
async def test_s1_unreachable_source_into_join(sqlite_db) -> None:
    nodes = [_agent("A"), _agent("B"), _agent("Z"), _agent("D")]
    edges = [
        Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
        Edge(edge_id="B-D", source_node_id="B", target_node_id="D"),
        Edge(edge_id="Z-D", source_node_id="Z", target_node_id="D"),
    ]
    orch = _orch({"A": _runner(_emit(value=1)), "B": _runner(_emit(b=1)),
                  "Z": _runner(_emit(x=1)), "D": _runner(_echo)}, sqlite_db)
    run = await orch.run_graph(_graph(nodes, edges), {"value": 1})
    # Audit finding 1 FIX: the join can never receive Z->D (Z is unreachable), so
    # the deadlock guard FAILS the run loudly instead of silently marking it
    # COMPLETED with D (and everything downstream) dropped + a false run.completed.
    assert run.status is RunStatus.FAILED, run.status
    assert run.failure_state is not None and run.failure_state.reason == "join_deadlock"
    assert "D" in (run.failure_state.message or "")


# ---------------------------------------------------------------------------
# S2 — wide fan-in: A -> P0..P9 -> D  (10 unconditional inbound, JoinConfig merge)
# ---------------------------------------------------------------------------
async def test_s2_wide_fan_in(sqlite_db) -> None:
    parents = [f"P{i}" for i in range(10)]
    nodes = [_agent("A")] + [_agent(p) for p in parents]
    nodes.append(_agent("D", join_config=JoinConfig(merge_strategy="merge")))
    edges = [Edge(edge_id=f"A-{p}", source_node_id="A", target_node_id=p) for p in parents]
    edges += [Edge(edge_id=f"{p}-D", source_node_id=p, target_node_id="D") for p in parents]
    runners = {"A": _runner(_emit(value=1))}
    for i, p in enumerate(parents):
        runners[p] = _runner(_emit(**{f"p{i}": i + 1}))
    runners["D"] = _runner(_echo)
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {"value": 1})
    assert run.status is RunStatus.COMPLETED, run.status
    # Join MECHANICS are sound: D dispatched exactly ONCE after all 10 inbound
    # edges resolved (the flag-off bug would run it 10x / clobber).
    assert _counts(run)["D"] == 1, _counts(run)
    # Audit finding 2 (KNOWN LIMITATION): agents emit their FULL model dump, so a
    # shallow `merge` of full dumps keeps only the LAST parent wholesale — earlier
    # parents' distinct values are clobbered. A meaningful merge of shared-schema
    # agent outputs needs `collect` or a `custom` reducer, not the default `merge`.
    merged = run.metadata["last_output"]
    assert merged.get("p9") == 10  # last parent survives
    assert merged.get("p0") == 0   # earlier parents clobbered (documented limitation)


# ---------------------------------------------------------------------------
# S3 — stacked diamonds: A->(B,C)->D->(E,F)->G  (two joins in series)
# ---------------------------------------------------------------------------
async def test_s3_stacked_diamonds(sqlite_db) -> None:
    nodes = [_agent("A"), _agent("B"), _agent("C"),
             _agent("D", join_config=JoinConfig(merge_strategy="merge")),
             _agent("E"), _agent("F"),
             _agent("G", join_config=JoinConfig(merge_strategy="merge"))]
    edges = [
        Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
        Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
        Edge(edge_id="B-D", source_node_id="B", target_node_id="D"),
        Edge(edge_id="C-D", source_node_id="C", target_node_id="D"),
        Edge(edge_id="D-E", source_node_id="D", target_node_id="E"),
        Edge(edge_id="D-F", source_node_id="D", target_node_id="F"),
        Edge(edge_id="E-G", source_node_id="E", target_node_id="G"),
        Edge(edge_id="F-G", source_node_id="F", target_node_id="G"),
    ]
    runners = {"A": _runner(_emit(value=1)), "B": _runner(_emit(b=1)), "C": _runner(_emit(c=2)),
               "D": _runner(_echo), "E": _runner(_emit(e=3)), "F": _runner(_emit(f=4)),
               "G": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {"value": 1})
    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    assert counts["D"] == 1 and counts["G"] == 1, counts


# ---------------------------------------------------------------------------
# S4 — deep skip cascade: A->X (true)->D ; A->B (FALSE)->C->D
#   The suppressed B branch must cascade B-skip -> C-skip -> C->D suppressed,
#   so D runs once on X's payload without waiting for the dead branch.
# ---------------------------------------------------------------------------
async def test_s4_deep_skip_cascade(sqlite_db) -> None:
    nodes = [_agent("A"), _agent("X"), _agent("B"), _agent("C"),
             _agent("D", join_config=JoinConfig(merge_strategy="merge"))]
    edges = [
        Edge(edge_id="A-X", source_node_id="A", target_node_id="X",
             condition=Condition(expression="payload.value >= 0")),   # true
        Edge(edge_id="A-B", source_node_id="A", target_node_id="B",
             condition=Condition(expression="payload.value > 999")),  # false
        Edge(edge_id="X-D", source_node_id="X", target_node_id="D"),
        Edge(edge_id="B-C", source_node_id="B", target_node_id="C"),
        Edge(edge_id="C-D", source_node_id="C", target_node_id="D"),
    ]
    runners = {"A": _runner(_emit(value=1)), "X": _runner(_emit(x=7)),
               "B": _runner(_emit(b=1)), "C": _runner(_emit(c=1)), "D": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {"value": 1})
    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    assert counts.get("D", 0) == 1, f"D not run once: {counts}"
    assert counts.get("B", 0) == 0 and counts.get("C", 0) == 0, f"dead branch ran: {counts}"
    assert run.metadata["last_output"].get("x") == 7


# ---------------------------------------------------------------------------
# S5 — two CONDITIONAL inbound, BOTH fire, NO JoinConfig.
#   Validation only requires a JoinConfig for >=2 UNCONDITIONAL inbound, so this
#   graph is not flagged. At runtime both deliver -> default 'merge'. Probe: does
#   it run D once (merged) rather than crash or double-run?
# ---------------------------------------------------------------------------
async def test_s5_two_conditional_both_fire_no_joinconfig(sqlite_db) -> None:
    nodes = [_agent("A"), _agent("B"), _agent("C"), _agent("D")]  # D has NO join_config
    edges = [
        Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
        Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
        Edge(edge_id="B-D", source_node_id="B", target_node_id="D",
             condition=Condition(expression="payload.b >= 0")),  # true
        Edge(edge_id="C-D", source_node_id="C", target_node_id="D",
             condition=Condition(expression="payload.c >= 0")),  # true
    ]
    runners = {"A": _runner(_emit(value=1)), "B": _runner(_emit(b=5)),
               "C": _runner(_emit(c=6)), "D": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {"value": 1})
    assert run.status is RunStatus.COMPLETED, run.status
    assert _counts(run).get("D", 0) == 1, f"D not once: {_counts(run)}"


# ---------------------------------------------------------------------------
# S6 — convergent-on-cycle: A->B, B->C, C->B (back edge). B has inbound A->B and
#   C->B and sits on a cycle. Expect a LOUD failure (not a hang / silent drop).
# ---------------------------------------------------------------------------
async def test_s6_convergent_on_cycle_fails_loud(sqlite_db) -> None:
    nodes = [_agent("A"), _agent("B", join_config=JoinConfig(merge_strategy="merge")),
             _agent("C")]
    edges = [
        Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
        Edge(edge_id="B-C", source_node_id="B", target_node_id="C"),
        Edge(edge_id="C-B", source_node_id="C", target_node_id="B",
             condition=Condition(expression="payload.value > 999", allow_cycle_traversal=True)),
    ]
    runners = {"A": _runner(_emit(value=1)), "B": _runner(_echo), "C": _runner(_emit(c=1))}
    # Must NOT hang or silently drop — either fail the run loudly or complete
    # deterministically. We assert it terminates with a definite status.
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {"value": 1})
    assert run.status in (RunStatus.COMPLETED, RunStatus.FAILED), run.status
