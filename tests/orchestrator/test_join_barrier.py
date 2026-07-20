"""B9 sequential join-barrier integration tests (each revert-checked).

The join barrier is gated behind ``execution_settings.sequential_join_enabled``
(default False). The diamond-payload-corruption bug it fixes exists *today* with
no flag, and the fix is dormant when the flag is off. So the revert-check for
each behavioral case is baked directly into the test as a **double assertion**:

* with the flag **ON**  → the corrected behavior (join dispatches once, merged);
* with the flag **OFF** → the bug still reproduces (join runs twice / clobbered).

That double assertion IS the proof the test catches the bug — reverting the diff
(flag off = dormant) makes the flag-OFF branch the without-fix baseline, so a test
that only checked the flag-ON path would pass trivially and prove nothing.

Design: ``.planning/b9-join-barrier-design.md`` (§9 test plan, cases 1-8).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.governance.approvals import ApprovalDecision, ApprovalRepository, ApprovalService
from zeroth.governance.audit import AuditRepository
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutionSettings,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.contracts.graph.validation_errors import ValidationCode
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.runtime.parallel.models import JoinConfig, ParallelConfig
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.runs import Run, RunStatus

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Permissive I/O models — every field defaults so an empty payload (the
# flag-OFF clobber case, where the join's second dispatch reads ``{}``) still
# validates and the node runs, letting us observe the "runs twice" bug.
# ---------------------------------------------------------------------------


class Bag(BaseModel):
    value: int = 0
    a: int = 0
    b: int = 0
    c: int = 0
    e: int = 0
    g: int = 0
    total: int = 0
    tier: str = ""


# Single-field output models so two branches serialize to DISJOINT dicts —
# agent output_data is the full model dump (defaulted fields included), so a
# shared-field model would let one branch's defaults clobber the other's value
# on merge, hiding whether the join actually combined both payloads.
class OutB(BaseModel):
    b: int = 0


class OutC(BaseModel):
    c: int = 0


class OutE(BaseModel):
    e: int = 0


class OutTotal(BaseModel):
    total: int = 0


class OutItems(BaseModel):
    items: list = []  # noqa: RUF012


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _runner(handler, output_model: type[BaseModel] = Bag) -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name="agent",
            instruction="test",
            model_name="governai:test",
            input_model=Bag,
            output_model=output_model,
        ),
        CallableProviderAdapter(handler),
    )


def _emit(**fields: int) -> object:
    """A provider handler that emits a fixed dict (ignores input)."""
    return lambda req: ProviderResponse(content=dict(fields))


def _agent(node_id: str, *, parallel_config: ParallelConfig | None = None) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref="join-test:v1",
        agent=AgentNodeData(instruction="test", model_provider=f"provider://{node_id}"),
        parallel_config=parallel_config,
    )


def _orchestrator(runners: dict[str, AgentRunner], sqlite_db, **kw) -> RuntimeOrchestrator:
    return RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners=runners,
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        **kw,
    )


def _graph(nodes, edges, *, flag: bool, entry: str = "A") -> Graph:
    return Graph(
        graph_id="join-test",
        name="join-test",
        entry_step=entry,
        execution_settings=ExecutionSettings(
            max_total_steps=60, sequential_join_enabled=flag
        ),
        nodes=nodes,
        edges=edges,
    )


def _counts(run: Run) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in run.execution_history:
        counts[entry.node_id] = counts.get(entry.node_id, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Balanced diamond builder:  A -> B, A -> C, B -> D, C -> D
#   B emits {b:10}, C emits {c:20}; D echoes its (merged) input.
# ---------------------------------------------------------------------------


def _diamond(*, flag: bool, join_config: JoinConfig | None) -> Graph:
    d = _agent("D")
    d.join_config = join_config
    return _graph(
        [_agent("A"), _agent("B"), _agent("C"), d],
        [
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            Edge(edge_id="B-D", source_node_id="B", target_node_id="D"),
            Edge(edge_id="C-D", source_node_id="C", target_node_id="D"),
        ],
        flag=flag,
    )


def _diamond_runners(sqlite_db) -> RuntimeOrchestrator:
    return _orchestrator(
        {
            "A": _runner(_emit(value=1)),
            "B": _runner(_emit(b=10), OutB),
            "C": _runner(_emit(c=20), OutC),
            # D echoes whatever it received so the merged payload is observable.
            "D": _runner(
                lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))
            ),
        },
        sqlite_db,
    )


# ===========================================================================
# Case 1 — Balanced diamond (revert-checked)
# ===========================================================================


async def test_balanced_diamond_flag_on_runs_once_merged(sqlite_db) -> None:
    """Flag ON: join D runs exactly once with the merge of both parents."""
    graph = _diamond(flag=True, join_config=JoinConfig(merge_strategy="merge"))
    run = await _diamond_runners(sqlite_db).run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    assert _counts(run)["D"] == 1
    # Merge of {b:10} and {c:20} — both survive (disjoint keys).
    assert run.metadata["last_output"]["b"] == 10
    assert run.metadata["last_output"]["c"] == 20


async def test_balanced_diamond_flag_off_reproduces_bug(sqlite_db) -> None:
    """Flag OFF (== without-fix baseline): D runs TWICE and the payload clobbers.

    This is the revert-check: it demonstrates the bug is real and that the
    flag-ON test above is what catches it.
    """
    graph = _diamond(flag=False, join_config=JoinConfig(merge_strategy="merge"))
    run = await _diamond_runners(sqlite_db).run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    # Bug: last-writer-wins clobber + double-append → D executes twice.
    assert _counts(run)["D"] == 2
    # And the second dispatch reads an empty payload (popped), so the final
    # output has lost both parents' data — corruption.
    assert run.metadata["last_output"].get("b", 0) == 0
    assert run.metadata["last_output"].get("c", 0) == 0


# ===========================================================================
# Case 2 — Unbalanced diamond:  A->B->D  and  A->C->E->D  (revert-checked)
# ===========================================================================


def _unbalanced(*, flag: bool) -> Graph:
    d = _agent("D")
    d.join_config = JoinConfig(merge_strategy="merge")
    return _graph(
        [_agent("A"), _agent("B"), _agent("C"), _agent("E"), d],
        [
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            Edge(edge_id="B-D", source_node_id="B", target_node_id="D"),
            Edge(edge_id="C-E", source_node_id="C", target_node_id="E"),
            Edge(edge_id="E-D", source_node_id="E", target_node_id="D"),
        ],
        flag=flag,
    )


def _unbalanced_orch(sqlite_db) -> RuntimeOrchestrator:
    return _orchestrator(
        {
            "A": _runner(_emit(value=1)),
            "B": _runner(_emit(b=10), OutB),
            "C": _runner(_emit(c=20), OutC),
            "E": _runner(_emit(e=30), OutE),
            "D": _runner(
                lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))
            ),
        },
        sqlite_db,
    )


async def test_unbalanced_diamond_flag_on_waits_for_slow_branch(sqlite_db) -> None:
    """Flag ON: the fast branch (B) does NOT dispatch D early; D waits for E."""
    run = await _unbalanced_orch(sqlite_db).run_graph(_unbalanced(flag=True), {"value": 1})

    assert run.status is RunStatus.COMPLETED
    assert _counts(run)["D"] == 1
    assert run.metadata["last_output"]["b"] == 10
    assert run.metadata["last_output"]["e"] == 30


async def test_unbalanced_diamond_flag_off_reproduces_bug(sqlite_db) -> None:
    """Flag OFF: D dispatches early on B, then again on E — runs twice."""
    run = await _unbalanced_orch(sqlite_db).run_graph(_unbalanced(flag=False), {"value": 1})

    assert run.status is RunStatus.COMPLETED
    assert _counts(run)["D"] == 2


# ===========================================================================
# Case 3 — Conditional reconvergence (vendor-dd shape): unchanged, no JoinConfig
# ===========================================================================


def _conditional_reconvergence(*, flag: bool) -> Graph:
    """A -> score; score -[low]-> report; score -[high]-> review -> report.

    Mutually exclusive: exactly one inbound edge of ``report`` delivers per run.
    ``report`` needs NO join_config (only one unconditional inbound).
    """
    return _graph(
        [_agent("A"), _agent("score"), _agent("review"), _agent("report")],
        [
            Edge(edge_id="A-score", source_node_id="A", target_node_id="score"),
            Edge(
                edge_id="score-review",
                source_node_id="score",
                target_node_id="review",
                condition=Condition(expression="payload.tier == 'high'"),
            ),
            Edge(
                edge_id="score-report",
                source_node_id="score",
                target_node_id="report",
                condition=Condition(expression="payload.tier == 'low'"),
            ),
            Edge(edge_id="review-report", source_node_id="review", target_node_id="report"),
        ],
        flag=flag,
        entry="A",
    )


def _conditional_orch(sqlite_db) -> RuntimeOrchestrator:
    return _orchestrator(
        {
            "A": _runner(_emit(value=1)),
            "score": _runner(lambda req: ProviderResponse(content={"value": 1, "b": 5})),
            "review": _runner(_emit(c=7)),
            "report": _runner(
                lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))
            ),
        },
        sqlite_db,
    )


@pytest.mark.parametrize("flag", [False, True])
async def test_conditional_reconvergence_unchanged(sqlite_db, flag: bool) -> None:
    """report dispatches once with the single delivered payload — flag on OR off.

    ``score`` emits tier='low', so exactly one inbound edge of ``report``
    (``score-report``) delivers; the high branch (``review``) never fires.
    """
    orch = _conditional_orch(sqlite_db)
    # Route score's output so the 'low' edge is taken.
    orch.agent_runners["score"] = _runner(
        lambda req: ProviderResponse(content={"tier": "low", "b": 5})
    )
    graph = _conditional_reconvergence(flag=flag)
    run = await orch.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    assert _counts(run)["report"] == 1
    assert "review" not in _counts(run)  # high branch never fired


async def test_conditional_reconvergence_validates_without_join_config(sqlite_db) -> None:
    """The vendor-dd shape raises NO join-related error with the flag ON.

    ``report`` has one conditional + one unconditional inbound edge, so it is not
    a genuine concurrent-delivery join and needs no JoinConfig. (Contract-ref
    validation errors are unrelated and out of scope here.)
    """
    graph = _conditional_reconvergence(flag=True)
    report = await GraphValidator().validate(graph)
    join_codes = {ValidationCode.MISSING_JOIN_CONFIG, ValidationCode.JOIN_ON_CYCLE}
    raised = {i.code for i in report.issues if i.severity.value == "error"}
    assert not (raised & join_codes)


# ===========================================================================
# Case 5 — Skip cascade:  A -[false]-> B -> D,  A -> C -> D
# ===========================================================================


async def test_skip_cascade_resolves_not_deadlock(sqlite_db) -> None:
    """A fully-suppressed branch resolves; the join dispatches on the survivor.

    Flag ON: A->B is suppressed (condition false) → B is SKIPPED → B->D cascades
    SUPPRESSED → D still dispatches once on the delivered C->D. If the skip
    cascade were broken, D would wait forever for B->D, the queue would empty,
    and the run would COMPLETE with D never executed — so asserting D ran once
    catches a broken cascade.
    """
    d = _agent("D")
    d.join_config = JoinConfig(merge_strategy="merge")
    graph = _graph(
        [_agent("A"), _agent("B"), _agent("C"), d],
        [
            Edge(
                edge_id="A-B",
                source_node_id="A",
                target_node_id="B",
                condition=Condition(expression="payload.value == 999"),  # never true
            ),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            Edge(edge_id="B-D", source_node_id="B", target_node_id="D"),
            Edge(edge_id="C-D", source_node_id="C", target_node_id="D"),
        ],
        flag=True,
    )
    orch = _orchestrator(
        {
            "A": _runner(_emit(value=1)),
            "B": _runner(_emit(b=10), OutB),
            "C": _runner(_emit(c=20), OutC),
            "D": _runner(
                lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))
            ),
        },
        sqlite_db,
    )
    run = await orch.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    assert _counts(run).get("B") is None  # B was skipped, never executed
    assert _counts(run)["D"] == 1
    assert run.metadata["last_output"]["c"] == 20
    assert run.metadata["last_output"]["b"] == 0  # B never contributed


# ===========================================================================
# Case 6 — Parallel interaction: a parallel_config fan-in node is unaffected
# ===========================================================================


async def test_parallel_fan_in_unaffected_by_join_barrier(sqlite_db) -> None:
    """A parallel_config source fans out/in normally under the flag (join defers)."""
    source = _agent("source", parallel_config=ParallelConfig(split_path="items"))
    graph = _graph(
        [source, _agent("sink")],
        [Edge(edge_id="s-sink", source_node_id="source", target_node_id="sink")],
        flag=True,
        entry="source",
    )
    orch = _orchestrator(
        {
            "source": _runner(
                lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": 2}]}), OutItems
            ),
            "sink": _runner(
                lambda req: ProviderResponse(
                    content={"value": req.metadata["input_payload"].get("x", 0) * 10}
                )
            ),
        },
        sqlite_db,
    )
    run = await orch.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    last = run.metadata.get("last_output", {})
    assert "items" in last
    assert len(last["items"]) == 2


# ===========================================================================
# Case 7 — Resume/interrupt: join_state round-trips through checkpoint
# ===========================================================================


async def test_join_state_survives_approval_pause_and_resume(sqlite_db) -> None:
    """A run paused mid-join (approval on one branch) resumes and completes once.

    A -> B -> gate(approval) -> D  and  A -> C -> D. When C delivers, D's join is
    partially resolved and persisted; the run then pauses at the approval gate.
    On resume the gate delivers and D dispatches exactly once with BOTH payloads —
    proving join_state round-tripped through the RunRepository checkpoint.
    """
    d = _agent("D")
    d.join_config = JoinConfig(merge_strategy="merge")
    graph = _graph(
        [
            _agent("A"),
            _agent("B"),
            _agent("C"),
            HumanApprovalNode(
                node_id="gate",
                graph_version_ref="join-test:v1",
                human_approval=HumanApprovalNodeData(
                    approval_policy_config={"allow_edits": True}
                ),
            ),
            d,
        ],
        [
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            Edge(edge_id="B-gate", source_node_id="B", target_node_id="gate"),
            Edge(edge_id="gate-D", source_node_id="gate", target_node_id="D"),
            Edge(edge_id="C-D", source_node_id="C", target_node_id="D"),
        ],
        flag=True,
    )
    approval_service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )
    orch = _orchestrator(
        {
            "A": _runner(_emit(value=1)),
            "B": _runner(_emit(b=10), OutB),
            "C": _runner(_emit(c=20), OutC),
            "D": _runner(
                lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))
            ),
        },
        sqlite_db,
        approval_service=approval_service,
        audit_repository=AuditRepository(sqlite_db),
    )

    paused = await orch.run_graph(graph, {"value": 1})
    assert paused.status is RunStatus.WAITING_APPROVAL
    # C already delivered into D's join before the pause — partial state persisted.
    assert "join_state" in paused.metadata
    assert "D" in paused.metadata["join_state"]

    approval_id = paused.metadata["pending_approval"]["approval_id"]
    await approval_service.resolve(
        approval_id,
        decision=ApprovalDecision.EDIT_AND_APPROVE,
        actor=ActorIdentity(subject="user-1", auth_method=AuthMethod.API_KEY),
        edited_payload={"g": 99},
    )
    resumed = await approval_service.continue_run(
        approval_id, graph=graph, orchestrator=orch
    )

    assert resumed.status is RunStatus.COMPLETED
    assert _counts(resumed)["D"] == 1
    # D saw BOTH the pre-pause C payload and the post-resume gate payload.
    assert resumed.metadata["last_output"]["c"] == 20
    assert resumed.metadata["last_output"]["g"] == 99


# ===========================================================================
# Case 8 — Merge policy: merge / reduce / custom + missing-config validation
# ===========================================================================


@pytest.mark.parametrize(
    ("join_config", "expected"),
    [
        (JoinConfig(merge_strategy="merge"), {"b": 10, "c": 20}),
        # reduce == built-in last-wins fold over [{b:10},{c:20}] → {c:20} (b dropped)
        (JoinConfig(merge_strategy="reduce"), {"c": 20}),
        # custom sum_scores folds {total:...} dicts
        (JoinConfig(merge_strategy="custom", reducer_ref="tests._fixtures.reducers.sum_scores"), None),
    ],
)
async def test_merge_policies(sqlite_db, join_config: JoinConfig, expected) -> None:
    """merge / reduce / custom each produce the expected combined payload."""
    d = _agent("D")
    d.join_config = join_config
    graph = _diamond_from(d, flag=True)
    if join_config.merge_strategy == "custom":
        # Route B and C to emit {total:...} for the summing reducer.
        orch = _orchestrator(
            {
                "A": _runner(_emit(value=1)),
                "B": _runner(_emit(total=5), OutTotal),
                "C": _runner(_emit(total=7), OutTotal),
                "D": _runner(
                    lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))
                ),
            },
            sqlite_db,
        )
        run = await orch.run_graph(graph, {"value": 1})
        assert run.status is RunStatus.COMPLETED
        assert run.metadata["last_output"]["total"] == 12
        return

    run = await _diamond_runners(sqlite_db).run_graph(graph, {"value": 1})
    assert run.status is RunStatus.COMPLETED
    assert _counts(run)["D"] == 1
    for key, val in expected.items():
        assert run.metadata["last_output"][key] == val
    if join_config.merge_strategy == "reduce":
        # last-wins fold drops the earlier branch's key
        assert run.metadata["last_output"]["b"] == 0


def _diamond_from(d: AgentNode, *, flag: bool) -> Graph:
    return _graph(
        [_agent("A"), _agent("B"), _agent("C"), d],
        [
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            Edge(edge_id="B-D", source_node_id="B", target_node_id="D"),
            Edge(edge_id="C-D", source_node_id="C", target_node_id="D"),
        ],
        flag=flag,
    )


async def test_multi_unconditional_inbound_without_join_config_fails_validation() -> None:
    """>=2 unconditional inbound edges with no JoinConfig → validation ERROR (flag on)."""
    graph = _diamond(flag=True, join_config=None)  # D has 2 unconditional inbound, no config
    report = await GraphValidator().validate(graph)
    codes = {i.code for i in report.issues if i.severity.value == "error"}
    assert ValidationCode.MISSING_JOIN_CONFIG in codes


async def test_missing_join_config_not_flagged_when_flag_off() -> None:
    """Flag OFF: the new validation is inert — an unconditional diamond still publishes."""
    graph = _diamond(flag=False, join_config=None)
    report = await GraphValidator().validate(graph)
    codes = {i.code for i in report.issues if i.severity.value == "error"}
    assert ValidationCode.MISSING_JOIN_CONFIG not in codes


# ===========================================================================
# Byte-identical guard — single-inbound nodes route through the join worklist
# when the flag is ON, but must behave identically to the flag-OFF legacy path.
# ===========================================================================


def _branching_graph(*, flag: bool) -> Graph:
    """No joins: A -> B (cond low) / A -> C (cond high) -> chain. Single-inbound only."""
    return _graph(
        [_agent("A"), _agent("B"), _agent("C"), _agent("Z")],
        [
            Edge(
                edge_id="A-B",
                source_node_id="A",
                target_node_id="B",
                condition=Condition(expression="payload.tier == 'low'"),
            ),
            Edge(
                edge_id="A-C",
                source_node_id="A",
                target_node_id="C",
                condition=Condition(expression="payload.tier == 'high'"),
            ),
            Edge(edge_id="B-Z", source_node_id="B", target_node_id="Z"),
        ],
        flag=flag,
    )


async def test_single_inbound_byte_identical_flag_on_vs_off(sqlite_db) -> None:
    """A plain branching graph produces identical order + last_output flag on vs off."""

    def build_orch():
        return _orchestrator(
            {
                "A": _runner(lambda req: ProviderResponse(content={"tier": "low", "value": 1})),
                "B": _runner(_emit(b=10)),
                "C": _runner(_emit(c=20)),
                "Z": _runner(
                    lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))
                ),
            },
            sqlite_db,
        )

    run_off = await build_orch().run_graph(_branching_graph(flag=False), {"value": 1})
    run_on = await build_orch().run_graph(_branching_graph(flag=True), {"value": 1})

    order_off = [e.node_id for e in run_off.execution_history]
    order_on = [e.node_id for e in run_on.execution_history]
    assert order_off == order_on == ["A", "B", "Z"]
    assert run_off.metadata["last_output"] == run_on.metadata["last_output"]
    assert run_off.status is run_on.status is RunStatus.COMPLETED


async def test_two_target_fanout_byte_identical_flag_on_vs_off(sqlite_db) -> None:
    """A source with TWO single-inbound targets preserves queue/execution order.

    This is the design's "single-inbound is the degenerate case" claim: with the
    flag ON these two targets still route through the join worklist, so we prove
    the worklist enqueues them in the same order (and produces the same
    last_output) as the legacy direct-queue path.
    """

    def build_orch():
        return _orchestrator(
            {
                "A": _runner(_emit(value=1)),
                "B": _runner(_emit(b=10), OutB),
                "C": _runner(_emit(c=20), OutC),
            },
            sqlite_db,
        )

    def graph(flag: bool) -> Graph:
        return _graph(
            [_agent("A"), _agent("B"), _agent("C")],
            [
                Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
                Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            ],
            flag=flag,
        )

    run_off = await build_orch().run_graph(graph(False), {"value": 1})
    run_on = await build_orch().run_graph(graph(True), {"value": 1})

    order_off = [e.node_id for e in run_off.execution_history]
    order_on = [e.node_id for e in run_on.execution_history]
    assert order_off == order_on == ["A", "B", "C"]
    assert run_off.metadata["last_output"] == run_on.metadata["last_output"]


# ===========================================================================
# Case 4 — Loop convergence (DEFERRED, design §4.4). Two pins:
#   (a) active: convergent-on-cycle is rejected loudly at validation under flag;
#   (b) xfail:  it SHOULD instead be supported (per-iteration re-join).
# ===========================================================================


def _loop_convergent_graph() -> Graph:
    """A convergent node ``loop`` inside a cycle: entry->loop, loop->work->loop.

    ``loop`` has 2 inbound edges (entry-loop, work-loop) → convergent AND on a
    cycle. Under the flag this must be rejected (per-iteration re-join deferred).
    """
    return Graph(
        graph_id="loop-join",
        name="loop-join",
        entry_step="A",
        execution_settings=ExecutionSettings(
            max_total_steps=30, max_visits_per_edge=3, sequential_join_enabled=True
        ),
        nodes=[_agent("A"), _agent("loop"), _agent("work")],
        edges=[
            Edge(edge_id="A-loop", source_node_id="A", target_node_id="loop"),
            Edge(
                edge_id="work-loop",
                source_node_id="work",
                target_node_id="loop",
                condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True),
            ),
            Edge(edge_id="loop-work", source_node_id="loop", target_node_id="work"),
        ],
    )


async def test_loop_convergent_node_rejected_at_validation() -> None:
    """Active pin: a convergent-on-cycle node fails validation loudly under the flag."""
    report = await GraphValidator().validate(_loop_convergent_graph())
    codes = {i.code for i in report.issues if i.severity.value == "error"}
    assert ValidationCode.JOIN_ON_CYCLE in codes


@pytest.mark.xfail(
    reason="B9 §4.4 per-iteration loop join scoping deferred — a convergent node "
    "inside a loop is currently rejected at validation instead of re-joining "
    "each iteration. Remove this xfail when loop scoping lands.",
    strict=True,
)
async def test_loop_reconvergence_should_re_join_each_iteration() -> None:
    """Deferred contract: a loop-convergent node SHOULD validate and re-join per iteration."""
    report = await GraphValidator().validate(_loop_convergent_graph())
    errors = [i for i in report.issues if i.severity.value == "error"]
    assert errors == []
