"""Unit tests for the B9 token-engine static analysis + provenance tags (P1).

Pure functions, no dispatch — these lock down loop-scope analysis and tag
arithmetic before the token join engine (P2/P3) is wired into the drive loop.
"""

from __future__ import annotations

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.runtime.orchestration import token_scope as ts
from zeroth.runtime.parallel.models import ParallelConfig


def _agent(nid: str, *, parallel: ParallelConfig | None = None) -> AgentNode:
    return AgentNode(
        node_id=nid,
        graph_version_ref="ts:v1",
        agent=AgentNodeData(instruction="t", model_provider=f"p://{nid}"),
        parallel_config=parallel,
    )


def _edge(src: str, dst: str, *, back: bool = False) -> Edge:
    cond = Condition(expression="payload.value < 3", allow_cycle_traversal=True) if back else None
    return Edge(edge_id=f"{src}-{dst}", source_node_id=src, target_node_id=dst, condition=cond)


def _graph(node_ids, edges, *, entry: str = "A") -> Graph:
    return Graph(
        graph_id="ts", name="ts", entry_step=entry,
        execution_settings=ExecutionSettings(sequential_join_enabled=True),
        nodes=[_agent(n) for n in node_ids], edges=edges,
    )


# ---------------------------------------------------------------------------
# back_edge_ids / loop_bodies / enclosing / exit_edges
# ---------------------------------------------------------------------------


def test_dag_has_no_loops():
    g = _graph(["A", "B", "C"], [_edge("A", "B"), _edge("A", "C"), _edge("B", "C")])
    scopes = ts.analyze(g)
    assert scopes.back_edges == frozenset()
    assert scopes.bodies == {}
    assert scopes.exit_edges == {}
    assert scopes.loops_of("B") == frozenset()


def test_self_loop_body_and_exit():
    #  A -> L ; L -> L (back) ; L -> OUT
    g = _graph(["A", "L", "OUT"],
               [_edge("A", "L"), _edge("L", "L", back=True), _edge("L", "OUT")])
    scopes = ts.analyze(g)
    assert scopes.back_edges == {"L-L"}
    assert scopes.bodies == {"L": frozenset({"L"})}
    assert scopes.loops_of("L") == frozenset({"L"})
    assert scopes.exit_edges == {"L": frozenset({"L-OUT"})}


def test_simple_loop_body_header_and_exit():
    #  A -> H ; H -> W ; W -> H (back) ; W -> OUT (exit)
    g = _graph(["A", "H", "W", "OUT"],
               [_edge("A", "H"), _edge("H", "W"), _edge("W", "H", back=True), _edge("W", "OUT")])
    scopes = ts.analyze(g)
    assert scopes.back_edges == {"W-H"}
    assert scopes.bodies == {"H": frozenset({"H", "W"})}
    assert scopes.loops_of("W") == frozenset({"H"})
    assert scopes.loops_of("OUT") == frozenset()
    # W->OUT leaves the loop; W->H is the back-edge (not an exit).
    assert scopes.exit_edges == {"H": frozenset({"W-OUT"})}


def test_nested_loops_bodies_and_exits():
    #  OH -> IH -> IB ; IB -> IH (inner back) ; IB -> OB ; OB -> OH (outer back) ; OB -> DONE
    g = _graph(["OH", "IH", "IB", "OB", "DONE"],
               [_edge("OH", "IH"), _edge("IH", "IB"), _edge("IB", "IH", back=True),
                _edge("IB", "OB"), _edge("OB", "OH", back=True), _edge("OB", "DONE")],
               entry="OH")
    scopes = ts.analyze(g)
    assert scopes.back_edges == {"IB-IH", "OB-OH"}
    # Inner loop headed by IH: body {IH, IB}. Outer headed by OH: whole cycle.
    assert scopes.bodies["IH"] == frozenset({"IH", "IB"})
    assert scopes.bodies["OH"] == frozenset({"OH", "IH", "IB", "OB"})
    # IB is inside BOTH loops; OB only the outer; DONE neither.
    assert scopes.loops_of("IB") == frozenset({"IH", "OH"})
    assert scopes.loops_of("OB") == frozenset({"OH"})
    assert scopes.loops_of("DONE") == frozenset()
    # IB->OB leaves the inner loop (IH) but stays in the outer -> exit of IH only.
    assert "IB-OB" in scopes.exit_edges["IH"]
    assert "IB-OB" not in scopes.exit_edges["OH"]
    # OB->DONE leaves the outer loop.
    assert "OB-DONE" in scopes.exit_edges["OH"]


# ---------------------------------------------------------------------------
# propagate_tag
# ---------------------------------------------------------------------------


def _edge_by(g: Graph, eid: str) -> Edge:
    return next(e for e in g.edges if e.edge_id == eid)


def test_tag_unchanged_on_plain_forward_edge():
    g = _graph(["A", "B", "C"], [_edge("A", "B"), _edge("B", "C")])
    scopes = ts.analyze(g)
    assert ts.propagate_tag(ts.INITIAL_TAG, _edge_by(g, "A-B"), scopes) == ()


def test_tag_enters_and_reenters_and_exits_a_loop():
    g = _graph(["A", "H", "W", "OUT"],
               [_edge("A", "H"), _edge("H", "W"), _edge("W", "H", back=True), _edge("W", "OUT")])
    scopes = ts.analyze(g)
    # Entering the loop: A->H pushes (H, 0).
    t_enter = ts.propagate_tag(ts.INITIAL_TAG, _edge_by(g, "A-H"), scopes)
    assert t_enter == (("H", 0),)
    # Within the loop: H->W keeps the tag.
    t_body = ts.propagate_tag(t_enter, _edge_by(g, "H-W"), scopes)
    assert t_body == (("H", 0),)
    # Back-edge W->H bumps H's iteration.
    t_reenter = ts.propagate_tag(t_body, _edge_by(g, "W-H"), scopes)
    assert t_reenter == (("H", 1),)
    # Exit edge W->OUT strips the loop -> outer scope.
    t_exit = ts.propagate_tag(t_body, _edge_by(g, "W-OUT"), scopes)
    assert t_exit == ()


def test_tag_nested_reentry_bumps_only_inner():
    g = _graph(["OH", "IH", "IB", "OB", "DONE"],
               [_edge("OH", "IH"), _edge("IH", "IB"), _edge("IB", "IH", back=True),
                _edge("IB", "OB"), _edge("OB", "OH", back=True), _edge("OB", "DONE")],
               entry="OH")
    scopes = ts.analyze(g)
    # A token deep in both loops at outer=2, inner=0:
    tag = (("IH", 0), ("OH", 2))
    # Inner back-edge IB->IH bumps only IH.
    t_inner = ts.propagate_tag(tag, _edge_by(g, "IB-IH"), scopes)
    assert dict(t_inner) == {"IH": 1, "OH": 2}
    # Outer back-edge OB->OH bumps OH; IB->OB first strips the inner loop, so from
    # OB the tag is {OH:2}; OB->OH -> {OH:3}.
    t_at_ob = ts.propagate_tag(tag, _edge_by(g, "IB-OB"), scopes)
    assert dict(t_at_ob) == {"OH": 2}  # inner stripped on exit of IH
    t_outer = ts.propagate_tag(t_at_ob, _edge_by(g, "OB-OH"), scopes)
    assert dict(t_outer) == {"OH": 3}


def test_tag_key_is_stable_and_readable():
    assert ts.tag_key(()) == "()"
    assert ts.tag_key((("H", 1),)) == "H:1"
    assert ts.tag_key((("IH", 0), ("OH", 2))) == "IH:0|OH:2"


# ---------------------------------------------------------------------------
# Consistency with the runtime's own back-edge classification.
# ---------------------------------------------------------------------------


def test_back_edges_match_runtime_classifier():
    from zeroth.integrations.persistence.runs import RunRepository
    from zeroth.runtime.orchestration import RuntimeOrchestrator

    g = _graph(["OH", "IH", "IB", "OB", "DONE"],
               [_edge("OH", "IH"), _edge("IH", "IB"), _edge("IB", "IH", back=True),
                _edge("IB", "OB"), _edge("OB", "OH", back=True), _edge("OB", "DONE")],
               entry="OH")
    orch = RuntimeOrchestrator(
        run_repository=RunRepository.__new__(RunRepository),  # never touched here
        agent_runners={},
        executable_unit_runner=None,  # type: ignore[arg-type]
    )
    # The token module and the live engine must classify loops identically.
    assert ts.back_edge_ids(g) == orch._driver._back_edge_ids(g)
