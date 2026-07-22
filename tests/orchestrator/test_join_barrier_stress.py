"""B9 join-barrier ADVERSARIAL STRESS suite (audit).

Self-contained harness (tests/orchestrator has no __init__, so nothing is
imported from test_join_barrier). Every graph runs with the flag ON and probes
an edge case the happy-path suite doesn't: unreachable-source deadlock, wide
fan-in, stacked diamonds, deep skip cascade, conditional-both-fire without a
JoinConfig, a diamond inside a loop, a parallel fan-out node inside a loop body,
a fan-out feeding a convergent node with a sequential co-parent, a parallel node
that is itself a join target, NESTED loops, a conditional branch that alternates
across iterations, a diamond feeding a loop header, and a loop-exit edge — the
loop-epoch model's full surface.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.runtime.parallel.models import JoinConfig, ParallelConfig
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.runs import RunStatus

pytestmark = pytest.mark.asyncio


class Bag(BaseModel):
    value: int = 0
    value2: int = 0
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
    # S8 fan-out-feeds-join fields
    m: int = 0
    seq: int = 0
    parents: list = []  # noqa: RUF012


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


def _graph(nodes, edges, *, entry="A", flag=True) -> Graph:
    return Graph(
        graph_id="stress", name="stress", entry_step=entry,
        execution_settings=ExecutionSettings(max_total_steps=200, sequential_join_enabled=flag),
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
# S6 — DIAMOND INSIDE A LOOP: a convergent node merges TWO forward parents on
#   EVERY iteration (the exact shape the retired JOIN_ON_CYCLE validation forbade).
#     S(entry) -> B, S -> C  (two forward parents)
#     B -> J,   C -> J        (J convergent, merges both — needs a JoinConfig)
#     J -> S (back-edge while value < 3)
#   Each pass: S increments value, B/C tag it, J merges {b,value}+{c,value}. This
#   is the per-iteration MERGE path — untested by the single-delivery loop case in
#   the happy-path suite, and newly legal-and-live now that convergent-on-cycle is
#   no longer rejected. Was previously (pre-loop-support) a loud validation reject.
# ---------------------------------------------------------------------------
def _inc_value(req):
    return ProviderResponse(content={"value": req.metadata["input_payload"].get("value", 0) + 1})


def _tag(field):
    def _handler(req):
        value = req.metadata["input_payload"].get("value", 0)
        return ProviderResponse(content={field: value, "value": value})
    return _handler


async def test_s6_diamond_inside_loop_merges_two_parents_each_iteration(sqlite_db) -> None:
    nodes = [
        _agent("S"),
        _agent("B"),
        _agent("C"),
        _agent("J", join_config=JoinConfig(merge_strategy="merge")),
    ]
    edges = [
        Edge(edge_id="S-B", source_node_id="S", target_node_id="B"),
        Edge(edge_id="S-C", source_node_id="S", target_node_id="C"),
        Edge(edge_id="B-J", source_node_id="B", target_node_id="J"),
        Edge(edge_id="C-J", source_node_id="C", target_node_id="J"),
        Edge(edge_id="J-S", source_node_id="J", target_node_id="S",
             condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True)),
    ]
    runners = {
        "S": _runner(_inc_value),
        "B": _runner(_tag("b")),
        "C": _runner(_tag("c")),
        "J": _runner(_echo),
    }
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges, entry="S"), {"value": 0})

    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    # value climbs 0->1->2->3; the loop runs while value<3, so 3 iterations.
    # The join-scoping contract is proven by the counts alone: J dispatched
    # exactly ONCE per iteration (3, not 6 from a double-dispatch, not a hang),
    # and BOTH forward parents ran on every iteration (B==C==J==3). If either
    # parent were dropped, or J double-dispatched, or the join deadlocked on the
    # back-edge, these counts would diverge.
    assert counts["S"] == 3, counts
    assert counts["B"] == 3, counts
    assert counts["C"] == 3, counts
    assert counts["J"] == 3, counts
    # value survives the shallow merge (both parents carry the same value), so it
    # tracks the loop exactly. (Non-clobber of DISTINCT parent fields is a
    # merge-policy concern proven by the collect test in test_join_barrier.py —
    # here `merge` deliberately keeps last-wins, so b/c are not asserted.)
    assert run.metadata["last_output"]["value"] == 3
    # Every per-iteration join scope drained — no orphaned state, so the
    # completion deadlock guard cannot false-fire.
    assert not run.metadata.get("join_state")


# ---------------------------------------------------------------------------
# S7 — PARALLEL FAN-OUT NODE INSIDE A LOOP BODY: composition of the parallel
#   subsystem with loop scoping (the literal "batch-parallelized loops" shape).
#     S(entry) -> F(parallel_config) ; F -> G(per-branch) ; G -> S (back-edge)
#   Each iteration re-enters the fan-out node F, which fans out over `items`,
#   runs G per branch, fans in, and loops back while count < 2.
# ---------------------------------------------------------------------------
async def test_s7_parallel_fanout_node_inside_loop(sqlite_db) -> None:
    nodes = [
        _agent("S"),
        _agent("F", parallel_config=ParallelConfig(split_path="items")),
        _agent("G"),
    ]
    edges = [
        Edge(edge_id="S-F", source_node_id="S", target_node_id="F"),
        Edge(edge_id="F-G", source_node_id="F", target_node_id="G"),
        Edge(edge_id="G-S", source_node_id="G", target_node_id="S",
             condition=Condition(expression="payload.value < 2", allow_cycle_traversal=True)),
    ]

    def _seed(req):
        # `value` is the loop counter (a real Bag field, so it survives coercion);
        # `items` is the list the fan-out node splits on each pass.
        value = req.metadata["input_payload"].get("value", 0)
        return ProviderResponse(content={"value": value + 1, "items": [{"x": 1}, {"x": 2}]})

    def _times_ten(req):
        return ProviderResponse(content={"x": req.metadata["input_payload"].get("x", 0) * 10})

    runners = {"S": _runner(_seed), "F": _runner(_echo), "G": _runner(_times_ten)}

    # A parallel fan-out node INSIDE a loop (a "batch-parallelized loop") spawns
    # multiple concurrent tokens that share one loop-iteration tag. The token join
    # engine does not yet correlate those (deferred to P4), so under the flag it is
    # REJECTED at publish rather than mis-executed.
    from zeroth.runtime.graph_validation import GraphValidator
    from zeroth.contracts.graph.validation_errors import ValidationCode

    on_codes = {
        i.code
        for i in (await GraphValidator().validate(_graph(nodes, edges, entry="S"))).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.FANOUT_IN_LOOP in on_codes, on_codes

    # The DEFAULT (legacy) engine still runs the batch-parallelized loop correctly,
    # so nothing regresses for users on the default path.
    run = await _orch(runners, sqlite_db).run_graph(
        _graph(nodes, edges, entry="S", flag=False), {"value": 0}
    )
    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    # value climbs 0->1->2; loop runs while value<2 → 2 iterations.
    assert counts["S"] == 2, counts
    assert counts["F"] == 2, counts
    # G runs once per branch (2 items) per iteration → 4 branch executions.
    assert counts["G"] == 4, counts


# ---------------------------------------------------------------------------
# S8 — FAN-OUT FEEDS A CONVERGENT NODE that ALSO has a sequential inbound
#   (B9 audit #4/#5). The post-fan-in continuation must enter the join barrier,
#   not the legacy clobber queue, so J joins BOTH the fan-out branch result and
#   the sequential edge and dispatches ONCE.
#     A(entry) -> SRC(parallel split items) -> MID(per-branch) -> J
#     A -> SEQ -> J ;  J.join_config = collect into `parents`
# ---------------------------------------------------------------------------
async def test_s8_fanout_and_sequential_edge_into_one_join(sqlite_db) -> None:
    nodes = [
        _agent("A"),
        _agent("SRC", parallel_config=ParallelConfig(split_path="items")),
        _agent("MID"),
        _agent("SEQ"),
        _agent("J", join_config=JoinConfig(merge_path="parents")),  # collect default
    ]
    edges = [
        Edge(edge_id="A-SRC", source_node_id="A", target_node_id="SRC"),
        Edge(edge_id="A-SEQ", source_node_id="A", target_node_id="SEQ"),
        Edge(edge_id="SRC-MID", source_node_id="SRC", target_node_id="MID"),
        Edge(edge_id="MID-J", source_node_id="MID", target_node_id="J"),
        Edge(edge_id="SEQ-J", source_node_id="SEQ", target_node_id="J"),
    ]
    runners = {
        "A": _runner(_emit(items=[{"m": 1}, {"m": 2}])),
        "SRC": _runner(_echo),
        "MID": _runner(lambda r: ProviderResponse(
            content={"m": r.metadata["input_payload"].get("m", 0) * 10})),
        "SEQ": _runner(_emit(seq=99)),
        "J": _runner(_echo),
    }
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {})

    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    # J dispatched EXACTLY ONCE (was 2 under the legacy clobber — double-dispatch).
    assert counts["J"] == 1, counts
    # J collected BOTH inbound: the fan-out branch result AND the sequential edge.
    parents = run.metadata["last_output"]["parents"]
    assert len(parents) == 2, parents
    # One parent carries the sequential payload (seq==99); the other carries the
    # fanned-in branch result (its own items list of MID outputs).
    seqs = {p.get("seq") for p in parents}
    assert 99 in seqs, parents


# ---------------------------------------------------------------------------
# S9 — a parallel_config node that is ALSO a >=2-inbound convergence (B9 #6).
#   Its INBOUND must be joined first, then it fans out ONCE over the merged
#   input — not once per delivering edge.
#     A -> B, A -> C ; B -> P, C -> P ; P(parallel split items, join collect) -> SINK
# ---------------------------------------------------------------------------
async def test_s9_parallel_node_as_join_target_fans_out_once(sqlite_db) -> None:
    nodes = [
        _agent("A"),
        _agent("B"),
        _agent("C"),
        _agent(
            "P",
            parallel_config=ParallelConfig(split_path="items"),
            join_config=JoinConfig(merge_path="items"),  # collect B,C into items
        ),
        _agent("SINK"),
    ]
    edges = [
        Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
        Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
        Edge(edge_id="B-P", source_node_id="B", target_node_id="P"),
        Edge(edge_id="C-P", source_node_id="C", target_node_id="P"),
        Edge(edge_id="P-SINK", source_node_id="P", target_node_id="SINK"),
    ]
    runners = {
        "A": _runner(_echo),
        "B": _runner(_emit(b=1)),
        "C": _runner(_emit(c=2)),
        "P": _runner(_echo),
        "SINK": _runner(_emit(x=1)),
    }
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {})

    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    # P joined B and C and fanned out EXACTLY ONCE (was: once per inbound edge).
    assert counts["P"] == 1, counts
    # The single fan-out split the collected [B, C] list → 2 branch executions.
    assert counts["SINK"] == 2, counts


async def test_s9b_multi_inbound_parallel_node_requires_join_config() -> None:
    """A >=2-unconditional-inbound parallel node with no JoinConfig fails validation.

    The parallel exemption from MISSING_JOIN_CONFIG is gone: a parallel node's
    inbound is joined before it fans out, so a genuine convergence must declare a
    merge policy.
    """
    from zeroth.runtime.graph_validation import GraphValidator
    from zeroth.contracts.graph.validation_errors import ValidationCode

    nodes = [
        _agent("A"),
        _agent("B"),
        _agent("C"),
        _agent("P", parallel_config=ParallelConfig(split_path="items")),  # NO join_config
    ]
    edges = [
        Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
        Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
        Edge(edge_id="B-P", source_node_id="B", target_node_id="P"),
        Edge(edge_id="C-P", source_node_id="C", target_node_id="P"),
    ]
    report = await GraphValidator().validate(_graph(nodes, edges))
    codes = {i.code for i in report.issues if i.severity.value == "error"}
    assert ValidationCode.MISSING_JOIN_CONFIG in codes


# ---------------------------------------------------------------------------
# S10 — NESTED LOOPS (B9 audit #1). An inner loop header re-entered via its
#   FORWARD edge on a later OUTER iteration must NOT be mistaken for a back-edge
#   re-entry. The pre-epoch model deadlocked this into a false join_deadlock.
#     OH(entry,inc)->IH->IB ; IB->IH(inner back) ; IB->OB ; OB->OH(outer back<2)
# ---------------------------------------------------------------------------
def _inc(field):
    def h(req):
        return ProviderResponse(content={field: req.metadata["input_payload"].get(field, 0) + 1})
    return h


async def test_s10_nested_loops_complete(sqlite_db) -> None:
    nodes = [_agent("OH"), _agent("IH"), _agent("IB"), _agent("OB")]
    edges = [
        Edge(edge_id="OH-IH", source_node_id="OH", target_node_id="IH"),
        Edge(edge_id="IH-IB", source_node_id="IH", target_node_id="IB"),
        Edge(edge_id="IB-IH", source_node_id="IB", target_node_id="IH",
             condition=Condition(expression="payload.value < 0", allow_cycle_traversal=True)),
        # The inner-loop exit is the MUTUALLY-EXCLUSIVE complement of the inner
        # back-edge (value < 0): so IB continues the inner loop XOR leaves it, never
        # both. An unconditional IB->OB alongside the conditional back-edge would be
        # a latent fork the publish guard (rightly) rejects; value >= 0 always holds
        # here, so runtime behaviour is unchanged. allow_cycle_traversal is required
        # because OB is revisited each OUTER iteration — the planner's cycle guard
        # only lets a CONDITIONAL edge re-enter an on-path node when it is set.
        Edge(edge_id="IB-OB", source_node_id="IB", target_node_id="OB",
             condition=Condition(expression="payload.value >= 0", allow_cycle_traversal=True)),
        Edge(edge_id="OB-OH", source_node_id="OB", target_node_id="OH",
             condition=Condition(expression="payload.value < 2", allow_cycle_traversal=True)),
    ]
    runners = {"OH": _runner(_inc("value")), "IH": _runner(_echo),
               "IB": _runner(_echo), "OB": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges, entry="OH"), {"value": 0})
    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    # Outer loop runs twice (value 0->1->2). Inner back-edge never fires, so each
    # node runs once per outer pass.
    assert counts == {"OH": 2, "IH": 2, "IB": 2, "OB": 2}, counts
    assert not run.metadata.get("join_state")
    # The outer back-edge fired at least once (value 0->1 re-enters), which
    # advances the token's OH iteration and re-freshens the inner header's join
    # tag — the mechanism that stops the false deadlock the counter model hit.
    assert run.metadata.get("edge_visit_counts", {}).get("OB-OH", 0) >= 1


# ---------------------------------------------------------------------------
# S11 — conditional branch that DELIVERS on iter 0 but is SUPPRESSED later
#   (B9 audit #2). The skip cascade must fire per epoch, not only on first visit.
#     S(entry,inc) -> B[value==1], S -> C[value>=2] ; B->J, C->J(merge) ; J->S(<3)
# ---------------------------------------------------------------------------
async def test_s11_conditional_branch_alternates_across_iterations(sqlite_db) -> None:
    nodes = [_agent("S"), _agent("B"), _agent("C"),
             _agent("J", join_config=JoinConfig(merge_strategy="merge"))]
    edges = [
        # Body-edge conditions inside a loop need allow_cycle_traversal so the
        # planner re-traverses them each iteration (the target is re-visited);
        # this is a branch-planner requirement, orthogonal to the join barrier.
        Edge(edge_id="S-B", source_node_id="S", target_node_id="B",
             condition=Condition(expression="payload.value == 1", allow_cycle_traversal=True)),
        Edge(edge_id="S-C", source_node_id="S", target_node_id="C",
             condition=Condition(expression="payload.value >= 2", allow_cycle_traversal=True)),
        Edge(edge_id="B-J", source_node_id="B", target_node_id="J"),
        Edge(edge_id="C-J", source_node_id="C", target_node_id="J"),
        Edge(edge_id="J-S", source_node_id="J", target_node_id="S",
             condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True)),
    ]
    runners = {"S": _runner(_inc("value")), "B": _runner(_echo),
               "C": _runner(_echo), "J": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges, entry="S"), {"value": 0})
    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    # value 0->1->2->3, loop while <3 → 3 iterations. J joins once per iteration.
    # B fires only when value==1 (iter0); C fires when value>=2 (iters 1,2).
    assert counts["S"] == 3, counts
    assert counts["J"] == 3, counts
    assert counts["B"] == 1, counts
    assert counts["C"] == 2, counts
    assert not run.metadata.get("join_state")


# ---------------------------------------------------------------------------
# S12 — DIAMOND FEEDING A LOOP HEADER (advisor case): a node with TWO forward
#   parents that ALSO has a back-edge. Exercises "forward_epoch excludes the loop
#   the node itself heads" together with a real 2-parent join on first entry.
#     A -> P1, A -> P2 ; P1 -> H, P2 -> H(merge) ; H -> W ; W -> H(back, value<2)
# ---------------------------------------------------------------------------
async def test_s12_diamond_feeds_a_loop_header(sqlite_db) -> None:
    nodes = [_agent("A"), _agent("P1"), _agent("P2"),
             _agent("H", join_config=JoinConfig(merge_strategy="merge")), _agent("W")]
    edges = [
        Edge(edge_id="A-P1", source_node_id="A", target_node_id="P1"),
        Edge(edge_id="A-P2", source_node_id="A", target_node_id="P2"),
        Edge(edge_id="P1-H", source_node_id="P1", target_node_id="H"),
        Edge(edge_id="P2-H", source_node_id="P2", target_node_id="H"),
        Edge(edge_id="H-W", source_node_id="H", target_node_id="W"),
        Edge(edge_id="W-H", source_node_id="W", target_node_id="H",
             condition=Condition(expression="payload.value < 2", allow_cycle_traversal=True)),
    ]
    runners = {
        "A": _runner(_emit(value=0)),
        "P1": _runner(lambda r: ProviderResponse(content={"p1": 1, "value": 0})),
        "P2": _runner(lambda r: ProviderResponse(content={"p2": 1, "value": 0})),
        "H": _runner(_echo),
        "W": _runner(_inc("value")),
    }
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {"value": 0})
    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    # H first-enters via the P1/P2 diamond join, then loops via W->H while value<2
    # (value 0->1->2): H and W run twice; the parents run once (outside the loop).
    assert counts["H"] == 2, counts
    assert counts["W"] == 2, counts
    assert counts["P1"] == 1 and counts["P2"] == 1, counts
    assert not run.metadata.get("join_state")


# ---------------------------------------------------------------------------
# S13 — LOOP-EXIT EDGE (advisor case): a node inside the loop conditionally loops
#   back OR exits to a node OUTSIDE the loop. The exit target's epoch is the empty
#   (DAG) tuple, stable across the loop's iterations, so its join must land there.
#     A -> H ; H -> W ; W -> H(back, value<2) ; W -> OUT(exit, value>=2)
# ---------------------------------------------------------------------------
async def test_s13_loop_exit_edge_to_downstream_node(sqlite_db) -> None:
    nodes = [_agent("A"), _agent("H"), _agent("W"), _agent("OUT")]
    edges = [
        Edge(edge_id="A-H", source_node_id="A", target_node_id="H"),
        Edge(edge_id="H-W", source_node_id="H", target_node_id="W"),
        Edge(edge_id="W-H", source_node_id="W", target_node_id="H",
             condition=Condition(expression="payload.value < 2", allow_cycle_traversal=True)),
        Edge(edge_id="W-OUT", source_node_id="W", target_node_id="OUT",
             condition=Condition(expression="payload.value >= 2")),
    ]
    runners = {"A": _runner(_emit(value=0)), "H": _runner(_echo),
               "W": _runner(_inc("value")), "OUT": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {"value": 0})
    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    # value climbs 0->1->2; loop while <2 → H,W run twice, then W exits to OUT once.
    assert counts["H"] == 2, counts
    assert counts["W"] == 2, counts
    assert counts["OUT"] == 1, counts
    assert run.metadata["last_output"]["value"] == 2
    assert not run.metadata.get("join_state")


# ---------------------------------------------------------------------------
# S14 — LOOP-THEN-COMBINE (audit re-review #2/#3): a join OUTSIDE the loop fed by
#   a pre-loop edge AND a loop-exit edge. The exit edge must resolve exactly once
#   (on loop exit), not once per iteration, or the join dispatches prematurely.
#     A -> Z ; A -> H ; H -> W ; W -> H(back, value<3) ; W -> Z(exit, value>=3) ; Z -> END
# ---------------------------------------------------------------------------
async def test_s14_loop_then_combine_out_of_loop_join(sqlite_db) -> None:
    nodes = [_agent("A"), _agent("H"), _agent("W"),
             _agent("Z", join_config=JoinConfig(merge_strategy="merge")), _agent("END")]
    edges = [
        Edge(edge_id="A-Z", source_node_id="A", target_node_id="Z"),
        Edge(edge_id="A-H", source_node_id="A", target_node_id="H"),
        Edge(edge_id="H-W", source_node_id="H", target_node_id="W"),
        Edge(edge_id="W-H", source_node_id="W", target_node_id="H",
             condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True)),
        Edge(edge_id="W-Z", source_node_id="W", target_node_id="Z",
             condition=Condition(expression="payload.value >= 3")),
        Edge(edge_id="Z-END", source_node_id="Z", target_node_id="END"),
    ]
    runners = {"A": _runner(_emit(value=0)), "H": _runner(_echo),
               "W": _runner(_inc("value")), "Z": _runner(_echo), "END": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {})
    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    # W loops until value reaches 3 (H,W run 3x). Z fires EXACTLY ONCE, after the
    # loop exits — not prematurely on iteration 0 with only the pre-loop payload.
    assert counts["H"] == 3 and counts["W"] == 3, counts
    assert counts["Z"] == 1, counts
    assert counts["END"] == 1, counts
    assert run.metadata["last_output"]["value"] == 3
    assert not run.metadata.get("join_state")


# ---------------------------------------------------------------------------
# S15/S16/S17 — the OTHER loop-then-combine encodings + multi-exit (round-3 #1-#4).
#   The loop-exit deferral must key on whether the loop genuinely CONTINUES (the
#   source took an active edge back into the loop body), covering do-while (S14),
#   header-tested (S15), decision-node (S16), AND multi-exit without hanging (S17).
# ---------------------------------------------------------------------------
async def test_s15_header_controlled_loop_then_combine(sqlite_db) -> None:
    # H decides continue (H->W, value<3) vs exit (H->Z, value>=3); latch W->H is
    # unconditional. The exit edge's source (H) takes no back-edge.
    nodes = [_agent("A"), _agent("H"), _agent("W"),
             _agent("Z", join_config=JoinConfig(merge_strategy="merge")), _agent("END")]
    edges = [
        Edge(edge_id="A-Z", source_node_id="A", target_node_id="Z"),
        Edge(edge_id="A-H", source_node_id="A", target_node_id="H"),
        Edge(edge_id="H-W", source_node_id="H", target_node_id="W",
             condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True)),
        Edge(edge_id="H-Z", source_node_id="H", target_node_id="Z",
             condition=Condition(expression="payload.value >= 3")),
        Edge(edge_id="W-H", source_node_id="W", target_node_id="H",
             condition=Condition(expression="True", allow_cycle_traversal=True)),
        Edge(edge_id="Z-END", source_node_id="Z", target_node_id="END"),
    ]
    runners = {"A": _runner(_emit(value=0)), "H": _runner(_echo),
               "W": _runner(_inc("value")), "Z": _runner(_echo), "END": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {})
    assert run.status is RunStatus.COMPLETED, run.status
    assert _counts(run)["Z"] == 1, _counts(run)
    assert run.metadata["last_output"]["value"] == 3
    assert not run.metadata.get("join_state")


async def test_s16_decision_node_loop_then_combine(sqlite_db) -> None:
    # A body node D decides exit (D->Z) vs continue (D->L); latch is L->H.
    nodes = [_agent("A"), _agent("H"), _agent("D"), _agent("L"),
             _agent("Z", join_config=JoinConfig(merge_strategy="merge")), _agent("END")]
    edges = [
        Edge(edge_id="A-Z", source_node_id="A", target_node_id="Z"),
        Edge(edge_id="A-H", source_node_id="A", target_node_id="H"),
        Edge(edge_id="H-D", source_node_id="H", target_node_id="D"),
        Edge(edge_id="D-Z", source_node_id="D", target_node_id="Z",
             condition=Condition(expression="payload.value >= 3")),
        Edge(edge_id="D-L", source_node_id="D", target_node_id="L",
             condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True)),
        Edge(edge_id="L-H", source_node_id="L", target_node_id="H",
             condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True)),
        Edge(edge_id="Z-END", source_node_id="Z", target_node_id="END"),
    ]
    runners = {"A": _runner(_emit(value=0)), "H": _runner(_echo), "D": _runner(_inc("value")),
               "L": _runner(_echo), "Z": _runner(_echo), "END": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {})
    assert run.status is RunStatus.COMPLETED, run.status
    assert _counts(run)["Z"] == 1, _counts(run)
    assert run.metadata["last_output"]["value"] == 3
    assert not run.metadata.get("join_state")


def _inc2(req):
    return ProviderResponse(content={"value": req.metadata["input_payload"].get("value", 0) + 2})


async def test_s17_multi_exit_loop_does_not_hang(sqlite_db) -> None:
    # The loop can exit to Z (value==3) OR to OUT (value>3). W steps by 2, jumping
    # 0->2->4, so it exits via OUT and Z's exit edge NEVER delivers. Z must still
    # complete (on the pre-loop A->Z payload) rather than deadlock on a deferred
    # exit edge that will never resolve.
    nodes = [_agent("A"), _agent("H"), _agent("W"),
             _agent("Z", join_config=JoinConfig(merge_strategy="merge")),
             _agent("OUT"), _agent("END"), _agent("END2")]
    edges = [
        Edge(edge_id="A-Z", source_node_id="A", target_node_id="Z"),
        Edge(edge_id="A-H", source_node_id="A", target_node_id="H"),
        Edge(edge_id="H-W", source_node_id="H", target_node_id="W"),
        Edge(edge_id="W-H", source_node_id="W", target_node_id="H",
             condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True)),
        Edge(edge_id="W-Z", source_node_id="W", target_node_id="Z",
             condition=Condition(expression="payload.value == 3")),
        Edge(edge_id="W-OUT", source_node_id="W", target_node_id="OUT",
             condition=Condition(expression="payload.value > 3")),
        Edge(edge_id="Z-END", source_node_id="Z", target_node_id="END"),
        Edge(edge_id="OUT-END2", source_node_id="OUT", target_node_id="END2"),
    ]
    runners = {"A": _runner(_emit(value=0)), "H": _runner(_echo), "W": _runner(_inc2),
               "Z": _runner(_echo), "OUT": _runner(_echo), "END": _runner(_echo),
               "END2": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(_graph(nodes, edges), {})
    assert run.status is RunStatus.COMPLETED, run.status
    # The loop exited via OUT; Z ran once (on A->Z) and OUT ran once — no deadlock.
    assert _counts(run)["Z"] == 1, _counts(run)
    assert _counts(run)["OUT"] == 1, _counts(run)
    assert not run.metadata.get("join_state")


# ===========================================================================
# S18-S20 — regression tests for the round-1 adversarial-review HIGH defects in
# the token exit-crossing arithmetic. Each ran GREEN with the flag OFF but
# false-deadlocked with it ON before the fix; the differential harness missed
# them (single fixed input / N<=4 / no nesting depth). See design doc.
# ===========================================================================
def _bump(field):
    """Increment one field while preserving the rest of the payload."""
    def h(req):
        d = dict(req.metadata["input_payload"])
        d[field] = d.get(field, 0) + 1
        return ProviderResponse(content=d)
    return h


def _cond(expr, *, cycle=False):
    return Condition(expression=expr, allow_cycle_traversal=cycle)


async def test_s18_loop_bypassed_by_precheck_completes(sqlite_db) -> None:
    # Review #1: a pre-check bypasses the loop entirely (A->J), so loop L (header
    # H) is never entered/crossed. Its exit edge W->J must still resolve (as
    # suppressed) or the out-of-loop join J waits forever and leaks join_state.
    nodes = [_agent("A"), _agent("H"), _agent("W"),
             _agent("J", join_config=JoinConfig(merge_strategy="merge")), _agent("END")]
    edges = [
        Edge(edge_id="A-J", source_node_id="A", target_node_id="J",
             condition=_cond("payload.value >= 0")),
        Edge(edge_id="A-H", source_node_id="A", target_node_id="H",
             condition=_cond("payload.value < 0")),
        Edge(edge_id="H-W", source_node_id="H", target_node_id="W"),
        Edge(edge_id="W-H", source_node_id="W", target_node_id="H",
             condition=_cond("payload.value < 3", cycle=True)),
        Edge(edge_id="W-J", source_node_id="W", target_node_id="J",
             condition=_cond("payload.value >= 3", cycle=True)),
        Edge(edge_id="J-END", source_node_id="J", target_node_id="END"),
    ]
    runners = {n: _runner(_echo) for n in ("A", "H", "W", "J", "END")}
    run = await _orch(runners, sqlite_db).run_graph(
        _graph(nodes, edges, entry="A"), {"value": 1})
    assert run.status is RunStatus.COMPLETED, run.status
    counts = _counts(run)
    assert counts["J"] == 1 and counts["END"] == 1, counts
    assert "H" not in counts and "W" not in counts, counts  # loop bypassed
    assert not run.metadata.get("join_state")  # no leak


async def test_s19_exit_edge_into_next_loops_header(sqlite_db) -> None:
    # Review #2: loop L1's exit edge lands on B, which is itself the header of a
    # second loop L2. B's two forward inbound (P->B plain, tX->B exit-enters-L2)
    # must key into the SAME bucket (both add B's (header,0)) — the strip-only tag
    # keyed them apart and deadlocked B.
    nodes = [_agent("S"), _agent("P"), _agent("A"), _agent("tX"),
             _agent("B", join_config=JoinConfig(merge_strategy="merge")),
             _agent("tY"), _agent("T")]
    edges = [
        Edge(edge_id="S-P", source_node_id="S", target_node_id="P"),
        Edge(edge_id="S-A", source_node_id="S", target_node_id="A"),
        Edge(edge_id="A-tX", source_node_id="A", target_node_id="tX"),
        Edge(edge_id="tX-A", source_node_id="tX", target_node_id="A",
             condition=_cond("payload.value < 2", cycle=True)),
        Edge(edge_id="tX-B", source_node_id="tX", target_node_id="B",
             condition=_cond("payload.value >= 2", cycle=True)),
        Edge(edge_id="P-B", source_node_id="P", target_node_id="B"),
        Edge(edge_id="B-tY", source_node_id="B", target_node_id="tY"),
        Edge(edge_id="tY-B", source_node_id="tY", target_node_id="B",
             condition=_cond("payload.value2 < 2", cycle=True)),
        Edge(edge_id="tY-T", source_node_id="tY", target_node_id="T",
             condition=_cond("payload.value2 >= 2", cycle=True)),
    ]
    runners = {"S": _runner(_echo), "P": _runner(_echo), "A": _runner(_bump("value")),
               "tX": _runner(_echo), "B": _runner(_echo), "tY": _runner(_bump("value2")),
               "T": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(
        _graph(nodes, edges, entry="S"), {"value": 0, "value2": 0})
    assert run.status is RunStatus.COMPLETED, run.status
    assert not run.metadata.get("join_state")


async def test_s20_nested_sibling_exits_share_out_of_loop_join(sqlite_db) -> None:
    # Review #3: nested loops (outer A, inner B) with a bail edge C->X that exits
    # BOTH and a sibling D->X that exits only the outer — both feed one join X.
    # The crossing must terminate every loop the active edge leaves and resolve
    # each exit at its target's scope, or X gets two divergent buckets.
    nodes = [_agent("A"), _agent("B"), _agent("C"), _agent("D"),
             _agent("X", join_config=JoinConfig(merge_strategy="merge"))]
    edges = [
        Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
        Edge(edge_id="B-C", source_node_id="B", target_node_id="C"),
        Edge(edge_id="C-B", source_node_id="C", target_node_id="B",
             condition=_cond("payload.value2 < 2", cycle=True)),
        Edge(edge_id="C-D", source_node_id="C", target_node_id="D",
             condition=_cond("payload.value2 >= 2 and payload.value < 2", cycle=True)),
        Edge(edge_id="C-X", source_node_id="C", target_node_id="X",
             condition=_cond("payload.value2 >= 2 and payload.value >= 2", cycle=True)),
        Edge(edge_id="D-A", source_node_id="D", target_node_id="A",
             condition=_cond("payload.value < 2", cycle=True)),
        Edge(edge_id="D-X", source_node_id="D", target_node_id="X",
             condition=_cond("payload.value >= 2", cycle=True)),
    ]
    runners = {"A": _runner(_bump("value")), "B": _runner(_echo), "C": _runner(_bump("value2")),
               "D": _runner(_echo), "X": _runner(_echo)}
    run = await _orch(runners, sqlite_db).run_graph(
        _graph(nodes, edges, entry="A"), {"value": 0, "value2": 0})
    assert run.status is RunStatus.COMPLETED, run.status
    assert not run.metadata.get("join_state")
