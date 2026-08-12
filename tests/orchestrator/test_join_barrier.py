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
from zeroth.contracts.graph.validation_errors import ValidationCode
from zeroth.governance.approvals import ApprovalDecision, ApprovalRepository, ApprovalService
from zeroth.governance.audit import AuditRepository
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.parallel.models import JoinConfig, ParallelConfig
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
        execution_settings=ExecutionSettings(max_total_steps=60, sequential_join_enabled=flag),
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
            "D": _runner(lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))),
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
            "D": _runner(lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))),
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
    """Report dispatches once with the single delivered payload — flag on OR off.

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
            "D": _runner(lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))),
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
                human_approval=HumanApprovalNodeData(approval_policy_config={"allow_edits": True}),
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
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
    )
    orch = _orchestrator(
        {
            "A": _runner(_emit(value=1)),
            "B": _runner(_emit(b=10), OutB),
            "C": _runner(_emit(c=20), OutC),
            "D": _runner(lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))),
        },
        sqlite_db,
        approval_service=approval_service,
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
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
    resumed = await approval_service.continue_run(approval_id, graph=graph, orchestrator=orch)

    assert resumed.status is RunStatus.COMPLETED
    assert _counts(resumed)["D"] == 1
    # D saw BOTH the pre-pause C payload and the post-resume gate payload.
    assert resumed.metadata["last_output"]["c"] == 20
    assert resumed.metadata["last_output"]["g"] == 99


# ===========================================================================
# Case 8a — collect is the DEFAULT: every parent arrives as a list element
# ===========================================================================


class BagItems(BaseModel):
    """Input model for a join node that receives a collected list."""

    value: int = 0
    items: list = []  # noqa: RUF012


def _items_runner() -> AgentRunner:
    """A join node that echoes the list it was handed, so arrival is observable."""
    return AgentRunner(
        AgentConfig(
            name="agent",
            instruction="test",
            model_name="governai:test",
            input_model=BagItems,
            output_model=OutItems,
        ),
        CallableProviderAdapter(
            lambda req: ProviderResponse(content={"items": req.metadata["input_payload"]["items"]})
        ),
    )


async def test_join_config_defaults_to_collect() -> None:
    """The default merge policy is the non-lossy one.

    Pinned separately from the behavioural test below because every other test in
    this module declares ``merge_strategy`` explicitly — so the *default* itself
    was previously unproven and could be changed without a single test failing.
    """
    assert JoinConfig().merge_strategy == "collect"
    assert JoinConfig().merge_path is None


async def test_default_join_collects_both_parents_into_a_list(sqlite_db) -> None:
    """A default-configured diamond delivers BOTH parents' payloads as list elements.

    This is the B9 Finding-2 fix. The previous ``merge`` default shallow-merged
    whole payloads, and agents emit their *entire* model dump (defaulted fields
    included) — so two parents sharing an output schema silently kept only the
    last parent's values. ``collect`` cannot lose a parent.

    Revert-check: with ``merge_strategy='merge'`` (the old default) D's payload
    has no ``items`` list at all and the parents are flattened together — the
    assertion below on two distinct elements fails.
    """
    d = _agent("D")
    d.join_config = JoinConfig(merge_path="items")  # merge_strategy defaults to collect
    graph = _diamond_from(d, flag=True)
    orch = _orchestrator(
        {
            "A": _runner(_emit(value=1)),
            "B": _runner(_emit(b=10), OutB),
            "C": _runner(_emit(c=20), OutC),
            "D": _items_runner(),
        },
        sqlite_db,
    )

    run = await orch.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    assert _counts(run)["D"] == 1
    items = run.metadata["last_output"]["items"]
    # Both parents arrive as their own element, in inbound-edge order — neither
    # overwrites the other.
    assert len(items) == 2
    assert items[0]["b"] == 10
    assert items[1]["c"] == 20


async def test_collect_shape_follows_config_not_delivery_count(sqlite_db) -> None:
    """A ``collect`` join yields a list even when only ONE inbound edge delivers.

    Shape must follow the *declared* config, never the runtime payload count:
    otherwise the same node hands downstream a list when both branches fire and a
    bare dict when only one does, and the downstream contract cannot be written.
    Here C's edge is conditioned false, so only B delivers.
    """
    d = _agent("D")
    d.join_config = JoinConfig(merge_path="items")
    graph = _graph(
        [_agent("A"), _agent("B"), _agent("C"), d],
        [
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            Edge(edge_id="B-D", source_node_id="B", target_node_id="D"),
            Edge(
                edge_id="C-D",
                source_node_id="C",
                target_node_id="D",
                condition=Condition(expression="payload.c > 999"),
            ),
        ],
        flag=True,
    )
    orch = _orchestrator(
        {
            "A": _runner(_emit(value=1)),
            "B": _runner(_emit(b=10), OutB),
            "C": _runner(_emit(c=20), OutC),
            "D": _items_runner(),
        },
        sqlite_db,
    )

    run = await orch.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    items = run.metadata["last_output"]["items"]
    assert len(items) == 1
    assert items[0]["b"] == 10


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
        (
            JoinConfig(merge_strategy="custom", reducer_ref="tests._fixtures.reducers.sum_scores"),
            None,
        ),
    ],
)
async def test_merge_policies(sqlite_db, join_config: JoinConfig, expected) -> None:
    """Merge / reduce / custom each produce the expected combined payload."""
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
# Case 4 — Loop convergence: a convergent node inside a cycle re-joins per
# iteration (design §4.4, now implemented — was previously rejected outright).
# ===========================================================================


def _loop_convergent_graph(*, flag: bool = True) -> Graph:
    """A convergent node ``loop`` inside a cycle: A->loop, loop->work, work->loop.

    ``loop`` has 2 inbound edges — ``A-loop`` (forward) and ``work-loop`` (the
    back-edge that closes the loop->work->loop cycle) — so it is convergent AND a
    loop header. Iteration 0 enters via ``A-loop``; iterations >=1 re-enter via
    ``work-loop`` alone. ``work`` increments ``value`` and the back-edge fires
    while ``value < 3``.
    """
    return Graph(
        graph_id="loop-join",
        name="loop-join",
        entry_step="A",
        execution_settings=ExecutionSettings(
            max_total_steps=30, max_visits_per_node=10, sequential_join_enabled=flag
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


async def test_loop_convergent_node_no_longer_rejected() -> None:
    """A convergent-on-cycle node is no longer flagged — JOIN_ON_CYCLE is retired.

    Revert-check: restoring the JOIN_ON_CYCLE validation makes this assertion
    fail. (The graph still carries unrelated contract-ref errors from the bare
    ``_agent`` helper, so we assert the *specific* code is absent, not that the
    report is clean.)
    """
    report = await GraphValidator().validate(_loop_convergent_graph())
    codes = {i.code for i in report.issues}
    assert ValidationCode.JOIN_ON_CYCLE not in codes


def _loop_runners(sqlite_db) -> RuntimeOrchestrator:
    def _increment(req):  # noqa: ANN001, ANN202
        value = req.metadata["input_payload"].get("value", 0)
        return ProviderResponse(content={"value": value + 1})

    return _orchestrator(
        {
            "A": _runner(_emit(value=0)),
            # loop is the convergent header: it echoes whatever payload it joined.
            "loop": _runner(
                lambda req: ProviderResponse(content=dict(req.metadata["input_payload"]))
            ),
            "work": _runner(_increment),
        },
        sqlite_db,
    )


async def test_loop_reconvergence_re_joins_each_iteration(sqlite_db) -> None:
    """A loop-header convergent node re-joins and re-runs on every iteration.

    ``value`` climbs 0->1->2->3; the ``work-loop`` back-edge fires while
    ``value < 3``. ``loop`` runs once per iteration (entered by ``A-loop`` on
    iteration 0, then by ``work-loop``), never deadlocking on the back-edge that
    cannot fire on the first visit and never mis-completing when it goes quiet.

    Revert-check: with the flag OFF the run still executes the loop the same
    number of times (the legacy path already handled single-inbound self-cycles),
    so the discriminating assertion is that the flag-ON path COMPLETES rather than
    raising ``OrchestratorError('resolved twice')`` — the exact failure this step
    fixes.
    """
    run = await _loop_runners(sqlite_db).run_graph(_loop_convergent_graph(flag=True), {"value": 0})

    assert run.status is RunStatus.COMPLETED
    counts = _counts(run)
    # loop entered on iterations 0,1,2 (value 0,1,2); work runs each time and
    # stops feeding the back-edge once value reaches 3.
    assert counts["loop"] == 3
    assert counts["work"] == 3
    assert run.metadata["last_output"]["value"] == 3
    # The barrier fully drained — no orphaned join scope left behind.
    assert not run.metadata.get("join_state")


async def test_loop_reconvergence_flag_off_also_completes(sqlite_db) -> None:
    """Legacy path (flag off) runs the same loop identically — no regression."""
    run = await _loop_runners(sqlite_db).run_graph(_loop_convergent_graph(flag=False), {"value": 0})
    assert run.status is RunStatus.COMPLETED
    counts = _counts(run)
    assert counts["loop"] == 3
    assert counts["work"] == 3


# ===========================================================================
# Reducibility guard: the loop-epoch model requires reducible control flow.
# ===========================================================================


def _irreducible_graph(*, flag: bool) -> Graph:
    """A cycle B<->C with TWO entry points (A->B and A->C) — irreducible.

    Neither B nor C dominates the other, so there is no single loop header; DFS
    back-edge classification would be order-dependent. The epoch model cannot run
    on this, so it must be rejected at publish.
    """
    return _graph(
        [_agent("A"), _agent("B"), _agent("C")],
        [
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            Edge(edge_id="A-C", source_node_id="A", target_node_id="C"),
            Edge(
                edge_id="B-C",
                source_node_id="B",
                target_node_id="C",
                condition=Condition(expression="payload.value < 1", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="C-B",
                source_node_id="C",
                target_node_id="B",
                condition=Condition(expression="payload.value < 1", allow_cycle_traversal=True),
            ),
        ],
        flag=flag,
    )


async def test_irreducible_loop_rejected_under_flag() -> None:
    """An irreducible loop fails publish validation loudly under the flag."""
    report = await GraphValidator().validate(_irreducible_graph(flag=True))
    codes = {i.code for i in report.issues if i.severity.value == "error"}
    assert ValidationCode.IRREDUCIBLE_LOOP in codes


async def test_irreducible_loop_not_checked_when_flag_off() -> None:
    """With the flag off the reducibility guard is dormant (byte-identical to pre-B9)."""
    report = await GraphValidator().validate(_irreducible_graph(flag=False))
    codes = {i.code for i in report.issues if i.severity.value == "error"}
    assert ValidationCode.IRREDUCIBLE_LOOP not in codes


async def test_reducible_nested_loop_accepted() -> None:
    """A properly reducible NESTED loop is NOT flagged as irreducible.

    OH->IH->IB, IB->IH (inner back-edge), IB->OB, OB->OH (outer back-edge). Both
    loops have a single dominating header, so this is reducible.
    """
    graph = _graph(
        [_agent("OH"), _agent("IH"), _agent("IB"), _agent("OB")],
        [
            Edge(edge_id="OH-IH", source_node_id="OH", target_node_id="IH"),
            Edge(edge_id="IH-IB", source_node_id="IH", target_node_id="IB"),
            Edge(
                edge_id="IB-IH",
                source_node_id="IB",
                target_node_id="IH",
                condition=Condition(expression="payload.value < 1", allow_cycle_traversal=True),
            ),
            Edge(edge_id="IB-OB", source_node_id="IB", target_node_id="OB"),
            Edge(
                edge_id="OB-OH",
                source_node_id="OB",
                target_node_id="OH",
                condition=Condition(expression="payload.value < 2", allow_cycle_traversal=True),
            ),
        ],
        flag=True,
        entry="OH",
    )
    report = await GraphValidator().validate(graph)
    codes = {i.code for i in report.issues if i.severity.value == "error"}
    assert ValidationCode.IRREDUCIBLE_LOOP not in codes


# ===========================================================================
# Re-review guards: multi-latch loops, fan-out-successor joins, unreachable loops.
# ===========================================================================


async def test_multi_latch_loop_rejected() -> None:
    """A loop header reached by TWO back-edges (two latches) fails publish (re-review #1).

    The single loop-epoch counter cannot key a header advanced by a latch that is
    not downstream of a body join, so this shape is rejected until a token engine.
    """
    graph = _graph(
        [
            _agent("A"),
            _agent("H"),
            _agent("X"),
            _agent("Y"),
            _agent("J"),
            _agent("L1"),
            _agent("L2"),
            _agent("M"),
        ],
        [
            Edge(edge_id="A-H", source_node_id="A", target_node_id="H"),
            Edge(edge_id="H-X", source_node_id="H", target_node_id="X"),
            Edge(edge_id="H-Y", source_node_id="H", target_node_id="Y"),
            Edge(edge_id="X-J", source_node_id="X", target_node_id="J"),
            Edge(edge_id="X-L2", source_node_id="X", target_node_id="L2"),
            Edge(edge_id="Y-M", source_node_id="Y", target_node_id="M"),
            Edge(edge_id="M-J", source_node_id="M", target_node_id="J"),
            Edge(edge_id="J-L1", source_node_id="J", target_node_id="L1"),
            Edge(
                edge_id="L1-H",
                source_node_id="L1",
                target_node_id="H",
                condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True),
            ),
            Edge(
                edge_id="L2-H",
                source_node_id="L2",
                target_node_id="H",
                condition=Condition(expression="payload.value < 3", allow_cycle_traversal=True),
            ),
        ],
        flag=True,
        entry="A",
    )
    codes = {
        i.code
        for i in (await GraphValidator().validate(graph)).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.MULTI_LATCH_LOOP in codes


async def test_fanout_successor_that_is_a_join_rejected() -> None:
    """A fan-out's immediate successor that is also a join target fails publish (re-review #4).

    The node would run per-branch AND be a sequential join — conflicting roles.
    The legitimate pattern puts the join one hop below the successor.
    """
    src = _agent("src", parallel_config=ParallelConfig(split_path="items"))
    d = _agent("D")
    d.join_config = JoinConfig(merge_path="parents")
    graph = _graph(
        [_agent("A"), src, _agent("X"), d],
        [
            Edge(edge_id="A-src", source_node_id="A", target_node_id="src"),
            Edge(edge_id="A-X", source_node_id="A", target_node_id="X"),
            Edge(edge_id="src-D", source_node_id="src", target_node_id="D"),
            Edge(edge_id="X-D", source_node_id="X", target_node_id="D"),
        ],
        flag=True,
        entry="A",
    )
    codes = {
        i.code
        for i in (await GraphValidator().validate(graph)).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.FANOUT_SUCCESSOR_JOIN in codes


async def test_fanout_inside_loop_rejected() -> None:
    """A parallel fan-out node inside a loop body fails publish (token engine P1).

    Fan-out in a loop produces concurrent tokens sharing one iteration tag, which
    the token join engine does not yet correlate — reject rather than mis-execute.
    """
    fan = _agent("F", parallel_config=ParallelConfig(split_path="items"))
    graph = _graph(
        [_agent("A"), fan, _agent("W")],
        [
            Edge(edge_id="A-F", source_node_id="A", target_node_id="F"),
            Edge(edge_id="F-W", source_node_id="F", target_node_id="W"),
            Edge(
                edge_id="W-F",
                source_node_id="W",
                target_node_id="F",
                condition=Condition(expression="payload.value < 2", allow_cycle_traversal=True),
            ),
        ],
        flag=True,
    )
    codes = {
        i.code
        for i in (await GraphValidator().validate(graph)).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.FANOUT_IN_LOOP in codes


async def test_fanout_outside_loop_not_flagged() -> None:
    """A fan-out node OUTSIDE every loop is unaffected by the P1 guard."""
    fan = _agent("F", parallel_config=ParallelConfig(split_path="items"))
    graph = _graph(
        [_agent("A"), fan, _agent("S")],
        [
            Edge(edge_id="A-F", source_node_id="A", target_node_id="F"),
            Edge(edge_id="F-S", source_node_id="F", target_node_id="S"),
        ],
        flag=True,
    )
    codes = {
        i.code
        for i in (await GraphValidator().validate(graph)).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.FANOUT_IN_LOOP not in codes


# ---------------------------------------------------------------------------
# The structural fan-out-in-loop guard: WELL-FORMED loops (a single circulating
# token — the shapes the token engine handles, mirroring stress S6/S10/S14-S17)
# must publish; MULTI-TOKEN FORKS (a node that splits the token) must be rejected.
# ---------------------------------------------------------------------------


def _e(src: str, dst: str, expr: str | None = None, *, cycle: bool = False) -> Edge:
    cond = None
    if expr is not None:
        cond = Condition(expression=expr, allow_cycle_traversal=cycle)
    return Edge(edge_id=f"{src}-{dst}", source_node_id=src, target_node_id=dst, condition=cond)


def _wellformed_loop_graphs() -> dict[str, Graph]:
    """One graph per well-formed loop shape the engine supports (single token)."""
    return {
        # S14 do-while: latch W->H(<3) XOR conditional exit W->Z(>=3).
        "do_while": _graph(
            [_agent("A"), _agent("H"), _agent("W"), _agent("Z"), _agent("END")],
            [
                _e("A", "Z"),
                _e("A", "H"),
                _e("H", "W"),
                _e("W", "H", "payload.value < 3", cycle=True),
                _e("W", "Z", "payload.value >= 3"),
                _e("Z", "END"),
            ],
            flag=True,
        ),
        # S15 header-tested: H decides continue H->W(<3) XOR exit H->Z(>=3).
        "header_tested": _graph(
            [_agent("A"), _agent("H"), _agent("W"), _agent("Z"), _agent("END")],
            [
                _e("A", "Z"),
                _e("A", "H"),
                _e("H", "W", "payload.value < 3", cycle=True),
                _e("H", "Z", "payload.value >= 3"),
                _e("W", "H", "True", cycle=True),
                _e("Z", "END"),
            ],
            flag=True,
        ),
        # S16 decision node D: exit D->Z(>=3) XOR continue D->L(<3), latch L->H.
        "decision_node": _graph(
            [_agent("A"), _agent("H"), _agent("D"), _agent("L"), _agent("Z"), _agent("END")],
            [
                _e("A", "Z"),
                _e("A", "H"),
                _e("H", "D"),
                _e("D", "Z", "payload.value >= 3"),
                _e("D", "L", "payload.value < 3", cycle=True),
                _e("L", "H", "payload.value < 3", cycle=True),
                _e("Z", "END"),
            ],
            flag=True,
        ),
        # S6 diamond inside a loop: H->B, H->C reconverge at D, D->H back.
        "diamond_in_loop": _graph(
            [_agent("H"), _agent("B"), _agent("C"), _agent("D")],
            [
                _e("H", "B"),
                _e("H", "C"),
                _e("B", "D"),
                _e("C", "D"),
                _e("D", "H", "payload.step < 5", cycle=True),
            ],
            flag=True,
            entry="H",
        ),
        # S17 multi-exit: latch W->H(<3), two conditional exits ==3 / >3.
        "multi_exit": _graph(
            [_agent("A"), _agent("H"), _agent("W"), _agent("Z"), _agent("OUT")],
            [
                _e("A", "H"),
                _e("H", "W"),
                _e("W", "H", "payload.value < 3", cycle=True),
                _e("W", "Z", "payload.value == 3"),
                _e("W", "OUT", "payload.value > 3"),
            ],
            flag=True,
        ),
        # S10 nested: inner IB->IH(<0) XOR conditional inner-exit IB->OB(>=0).
        # IB->OB needs cycle traversal — OB is revisited each outer iteration.
        "nested": _graph(
            [_agent("OH"), _agent("IH"), _agent("IB"), _agent("OB")],
            [
                _e("OH", "IH"),
                _e("IH", "IB"),
                _e("IB", "IH", "payload.value < 0", cycle=True),
                _e("IB", "OB", "payload.value >= 0", cycle=True),
                _e("OB", "OH", "payload.value < 2", cycle=True),
            ],
            flag=True,
            entry="OH",
        ),
    }


def _fork_loop_graphs() -> dict[str, Graph]:
    """One graph per multi-token FORK shape that must be rejected at publish."""
    return {
        # Latch W->H(<3) AND an UNCONDITIONAL exit W->Z fire together.
        "uncond_exit": _graph(
            [_agent("A"), _agent("H"), _agent("W"), _agent("Z")],
            [
                _e("A", "H"),
                _e("H", "W"),
                _e("W", "H", "payload.step < 5", cycle=True),
                _e("W", "Z"),
            ],
            flag=True,
        ),
        # Back-edge D->B AND unconditional D->C fork into the loop and out of it.
        "tail_fork": _graph(
            [_agent("A"), _agent("B"), _agent("C"), _agent("D")],
            [
                _e("A", "B"),
                _e("B", "D"),
                _e("C", "D"),
                _e("D", "B", "payload.step < 5", cycle=True),
                _e("D", "C"),
            ],
            flag=True,
        ),
        # Two back-edges from D re-enter two nested headers at once.
        "two_back_edges": _graph(
            [_agent("A"), _agent("B"), _agent("C"), _agent("D")],
            [
                _e("A", "B"),
                _e("B", "C"),
                _e("C", "D"),
                _e("D", "B", "payload.step < 5", cycle=True),
                _e("D", "C", "payload.step < 5", cycle=True),
            ],
            flag=True,
        ),
        # H forks one loop token into two arms. Continuing arms reconverge at L,
        # but on the terminal iteration both bypass L and leave the loop for the
        # same out-of-loop join. The exiting tokens do not reconverge before the
        # boundary, so the token engine cannot safely deduplicate J (D3).
        "exiting_diamond": _graph(
            [_agent("H"), _agent("B"), _agent("C"), _agent("L"), _agent("J"), _agent("END")],
            [
                _e("H", "B"),
                _e("H", "C"),
                _e("B", "L", "payload.step < 2", cycle=True),
                _e("C", "L", "payload.step < 2", cycle=True),
                _e("L", "H", "True", cycle=True),
                _e("B", "J", "payload.step >= 2"),
                _e("C", "J", "payload.step >= 2"),
                _e("J", "END"),
            ],
            flag=True,
            entry="H",
        ),
        # Unconditional exit B->OUT alongside a conditional in-loop branch B->X.
        "uncond_exit_cond_branch": _graph(
            [_agent("H"), _agent("B"), _agent("X"), _agent("OUT")],
            [
                _e("H", "B"),
                _e("B", "H", "payload.step < 5", cycle=True),
                _e("B", "X", "payload.step < 2"),
                _e("B", "OUT"),
            ],
            flag=True,
            entry="H",
        ),
    }


@pytest.mark.parametrize("name", list(_wellformed_loop_graphs()))
async def test_wellformed_loops_pass_fanout_guard(name: str) -> None:
    """Every single-token loop shape the engine supports must publish clean."""
    graph = _wellformed_loop_graphs()[name]
    codes = {
        i.code
        for i in (await GraphValidator().validate(graph)).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.FANOUT_IN_LOOP not in codes, f"{name} wrongly flagged as fan-out"


@pytest.mark.parametrize("name", list(_fork_loop_graphs()))
async def test_multi_token_fork_shapes_rejected(name: str) -> None:
    """Every shape that splits the single circulating token must fail publish."""
    graph = _fork_loop_graphs()[name]
    codes = {
        i.code
        for i in (await GraphValidator().validate(graph)).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.FANOUT_IN_LOOP in codes, f"{name} not caught as fan-out"


@pytest.mark.legacy_engine
async def test_exiting_diamond_guard_is_inert_when_token_engine_disabled() -> None:
    """D3's conservative publish guard must not change legacy validation."""
    graph = _fork_loop_graphs()["exiting_diamond"].model_copy(
        update={"execution_settings": ExecutionSettings(sequential_join_enabled=False)}
    )
    codes = {
        i.code
        for i in (await GraphValidator().validate(graph)).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.FANOUT_IN_LOOP not in codes


async def test_disabled_edge_does_not_hide_loop_fork() -> None:
    """Review #2 (round 2): the fan-out guard must model loops from ENABLED edges.

    only — the runtime (token_scope) drops disabled edges. A disabled back-edge
    that inflates the loop body would hide a real fork: X unconditionally forks to
    Y (in the loop) and Z (outside it) every iteration, but a disabled Z->Y pulls
    Z into the body and makes it look like a diamond. The guard must still fire.
    """

    def _mk(*, z_to_y_enabled: bool) -> Graph:
        return _graph(
            [_agent("H"), _agent("X"), _agent("Y"), _agent("Z")],
            [
                _e("H", "X"),
                _e("X", "Y"),
                _e("X", "Z"),
                _e("Y", "H", "payload.step < 3", cycle=True),
                Edge(edge_id="Z-Y", source_node_id="Z", target_node_id="Y", enabled=z_to_y_enabled),
            ],
            flag=True,
            entry="H",
        )

    # Disabled Z->Y: Z is outside the loop → X forks (Y continues, Z exits) → reject.
    codes = {
        i.code
        for i in (await GraphValidator().validate(_mk(z_to_y_enabled=False))).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.FANOUT_IN_LOOP in codes, codes


async def test_unreachable_loop_not_flagged_irreducible() -> None:
    """A reducible loop in an UNREACHABLE component is not wrongly flagged (re-review #7).

    Dominators are defined only over the reachable subgraph; the guard must skip
    unreachable back-edges rather than misread them as a residual cycle.
    """
    graph = _graph(
        [_agent("A"), _agent("B"), _agent("U"), _agent("V")],
        [
            Edge(edge_id="A-B", source_node_id="A", target_node_id="B"),
            # U<->V is an unreachable reducible loop (no edge from the reachable set).
            Edge(edge_id="U-V", source_node_id="U", target_node_id="V"),
            Edge(
                edge_id="V-U",
                source_node_id="V",
                target_node_id="U",
                condition=Condition(expression="payload.value < 1", allow_cycle_traversal=True),
            ),
        ],
        flag=True,
        entry="A",
    )
    codes = {
        i.code
        for i in (await GraphValidator().validate(graph)).issues
        if i.severity.value == "error"
    }
    assert ValidationCode.IRREDUCIBLE_LOOP not in codes
