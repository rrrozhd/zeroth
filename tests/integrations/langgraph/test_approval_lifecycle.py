"""Durable approval lifecycle regressions for governed LangGraph tools."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import pytest

from zeroth.integrations.langgraph import _approval_lifecycle as lifecycle_module
from zeroth.integrations.langgraph._approval_lifecycle import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalResolution,
    ApprovalState,
    SQLiteApprovalRepository,
)
from zeroth.integrations.langgraph._tool_errors import (
    ApprovalRequiresThreadError,
    PolicyViolation,
    ToolGovernanceError,
)
from zeroth.integrations.langgraph._tool_guard import guard_tool_call
from zeroth.integrations.langgraph._tool_normalize import normalize_tool_action
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)

CONTEXT = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-1",
    run_id="run-1",
    thread_id="thread-1",
)
ACTION = normalize_tool_action(
    name="delete_record",
    arguments={"table": "invoices", "id": 41},
    context=CONTEXT,
    side_effect=SideEffectClass.SIDE_EFFECTING,
    tool_call_id="call-1",
)
HOLD = ToolDecision(
    kind=ToolDecisionKind.REQUIRE_APPROVAL,
    reason_code="policy_violation",
    approval_ref="approval-7",
)
ALLOW = ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")
DENY = ToolDecision(kind=ToolDecisionKind.DENY, reason_code="policy_violation")


class SuspendedError(Exception):
    """Test pause signal."""


@dataclass(frozen=True)
class Interrupt:
    value: Any
    id: str


@dataclass(frozen=True)
class StateSnapshot:
    values: dict[str, Any]
    next: tuple[str, ...]
    config: dict[str, Any]
    metadata: dict[str, Any]
    created_at: str | None
    parent_config: dict[str, Any] | None
    tasks: tuple[Any, ...]
    interrupts: tuple[Interrupt, ...]


@dataclass(frozen=True)
class FakeCommand:
    resume: Any


@pytest.fixture(autouse=True)
def _command_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle_module, "_langgraph_command", FakeCommand)


@dataclass
class Pause:
    payload: dict[str, Any] | None = None

    def __call__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        raise SuspendedError


@dataclass
class SequencedClient:
    fresh: ToolDecision = ALLOW
    actions: list[dict[str, Any]] = field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        self.actions.append(dict(action.arguments))
        return self.fresh if len(self.actions) >= 3 else HOLD


def _snapshot(
    payload: dict[str, Any] | None, *, checkpoint_id: str = "checkpoint-1"
) -> StateSnapshot:
    interrupts = () if payload is None else (Interrupt(value=payload, id="interrupt-1"),)
    return StateSnapshot(
        values={},
        next=() if payload is None else ("tools",),
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": checkpoint_id}},
        metadata={},
        created_at=None,
        parent_config=None,
        tasks=(),
        interrupts=interrupts,
    )


@dataclass
class Graph:
    state: StateSnapshot
    callback: Any = None
    calls: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)

    def get_state(self, config: dict[str, Any]) -> StateSnapshot:
        assert config == {"configurable": {"thread_id": "thread-1"}}
        return self.state

    def invoke(self, command: Any, config: dict[str, Any]) -> Any:
        self.calls.append((command, config))
        result = None if self.callback is None else self.callback(command.resume)
        self.state = _snapshot(None, checkpoint_id="checkpoint-2")
        return result


def _request(repository: SQLiteApprovalRepository, client: Any, pause: Pause) -> None:
    with pytest.raises(SuspendedError):
        guard_tool_call(
            ACTION,
            CONTEXT,
            lambda: pytest.fail("tool ran before approval"),
            client=client,
            interrupt=pause,
            approval_lifecycle=repository,
        )


def _ready(
    tmp_path: Any, client: Any | None = None
) -> tuple[SQLiteApprovalRepository, ApprovalCoordinator, Graph, SequencedClient]:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    policy = client or SequencedClient()
    pause = Pause()
    _request(repository, policy, pause)
    assert pause.payload is not None
    graph = Graph(_snapshot(pause.payload))
    coordinator = ApprovalCoordinator(repository)
    coordinator.confirm_checkpoint("approval-7", graph)
    return repository, coordinator, graph, policy


def test_payload_round_trip_is_persisted_before_interrupt(tmp_path: Any) -> None:
    path = tmp_path / "approvals.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE approvals (marker TEXT)")
        connection.execute("INSERT INTO approvals VALUES ('core-table')")
    repository = SQLiteApprovalRepository(path)
    pause = Pause()

    _request(repository, SequencedClient(), pause)

    record = repository.get("approval-7")
    assert record.state is ApprovalState.AWAITING_CHECKPOINT
    assert dict(record.intent.payload) == pause.payload
    assert dict(record.intent.arguments) == {"table": "invoices", "id": 41}
    reopened = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3").get("approval-7")
    assert reopened.intent == record.intent
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT marker FROM approvals").fetchone() == ("core-table",)
    invalid = ApprovalResolution("approval-7", ApprovalDecision.APPROVE).to_payload()
    invalid["version"] = True
    with pytest.raises(ToolGovernanceError):
        ApprovalResolution.from_payload(invalid)


def test_lifecycle_rejects_invalid_transitions_and_expires_bounded_work(tmp_path: Any) -> None:
    now = [10.0]
    repository = SQLiteApprovalRepository(
        tmp_path / "approvals.sqlite3", ttl_seconds=5, clock=lambda: now[0]
    )
    pause = Pause()
    _request(repository, SequencedClient(), pause)

    with pytest.raises(ToolGovernanceError):
        repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))
    refused = repository.events("approval-7")[-1]
    assert not refused.accepted and refused.to_state is ApprovalState.DECIDED

    now[0] = 16.0
    [expired] = repository.expire_due(limit=1)
    assert expired.state is ApprovalState.EXPIRED
    assert repository.pending(limit=1) == ()


def test_checkpoint_must_match_before_an_approval_becomes_ready(tmp_path: Any) -> None:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    pause = Pause()
    _request(repository, SequencedClient(), pause)
    assert pause.payload is not None
    coordinator = ApprovalCoordinator(repository)

    with pytest.raises(ToolGovernanceError):
        coordinator.confirm_checkpoint("approval-7", Graph(_snapshot({"other": "interrupt"})))

    assert repository.get("approval-7").state is ApprovalState.ORPHANED


def test_idempotent_begin_decision_and_claim_do_not_duplicate_work(tmp_path: Any) -> None:
    repository, coordinator, graph, _ = _ready(tmp_path)
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(repository.decide, (resolution, resolution)))
    assert decisions[0] == decisions[1]
    with pytest.raises(ToolGovernanceError):
        repository.decide(ApprovalResolution("approval-7", ApprovalDecision.REJECT))
    with ThreadPoolExecutor(max_workers=2) as pool:
        resumes = list(
            pool.map(
                lambda _attempt: coordinator.resume("approval-7", graph, owner="worker-1"),
                (1, 2),
            )
        )
    assert all(
        record.state in (ApprovalState.RESUMING, ApprovalState.RESOLVED) for record in resumes
    )
    assert repository.get("approval-7").state is ApprovalState.RESOLVED
    assert len(graph.calls) == 1


@pytest.mark.parametrize(
    ("resolution", "fresh", "expected"),
    [
        (ApprovalResolution("approval-7", ApprovalDecision.REJECT), ALLOW, []),
        (ApprovalResolution("approval-7", ApprovalDecision.APPROVE), DENY, []),
        (
            ApprovalResolution(
                "approval-7", ApprovalDecision.APPROVE, {"table": "invoices", "id": 42}
            ),
            ALLOW,
            [{"table": "invoices", "id": 42}],
        ),
    ],
)
def test_revalidation_uses_edited_arguments_and_fresh_deny_wins(
    tmp_path: Any,
    resolution: ApprovalResolution,
    fresh: ToolDecision,
    expected: list[dict[str, Any]],
) -> None:
    policy = SequencedClient(fresh=fresh)
    repository, coordinator, graph, _ = _ready(tmp_path, policy)
    executed: list[dict[str, Any]] = []

    def resume(value: Any) -> None:
        guard_tool_call(
            ACTION,
            CONTEXT,
            lambda: executed.append(dict(ACTION.arguments)),
            client=policy,
            interrupt=lambda _payload: value,
            approval_lifecycle=repository,
            invoke_with_arguments=lambda arguments: executed.append(dict(arguments)),
        )

    graph.callback = resume
    repository.decide(resolution)
    coordinator.resume("approval-7", graph, owner="worker-1")

    assert executed == expected
    assert repository.get("approval-7").state is ApprovalState.RESOLVED
    if resolution.arguments is not None:
        assert policy.actions[-1] == dict(resolution.arguments)


def test_restart_after_resume_claim_inspects_checkpoint_before_retry(tmp_path: Any) -> None:
    repository, coordinator, graph, _ = _ready(tmp_path)
    repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))

    def crash_after_resume(_value: Any) -> None:
        graph.state = _snapshot(None, checkpoint_id="checkpoint-2")
        raise KeyboardInterrupt

    graph.callback = crash_after_resume
    with pytest.raises(KeyboardInterrupt):
        coordinator.resume("approval-7", graph, owner="worker-1")

    reopened = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    ApprovalCoordinator(reopened).resume("approval-7", graph, owner="worker-2")
    assert reopened.get("approval-7").state is ApprovalState.RESOLVED
    assert len(graph.calls) == 1


def test_original_thread_and_checkpoint_receive_a_langgraph_command(tmp_path: Any) -> None:
    repository, coordinator, graph, _ = _ready(tmp_path)
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)
    repository.decide(resolution)

    coordinator.resume("approval-7", graph, owner="worker-1")

    [(command, config)] = graph.calls
    assert isinstance(command, FakeCommand)
    assert command.resume == resolution.to_payload()
    assert config == {"configurable": {"thread_id": "thread-1", "checkpoint_id": "checkpoint-1"}}


def test_stateless_or_threadless_approval_fails_closed(tmp_path: Any) -> None:
    calls: list[str] = []
    for context, lifecycle in (
        (CONTEXT, None),
        (
            ToolGovernanceContext("tenant-a", "principal-1", "run-1"),
            SQLiteApprovalRepository(tmp_path / "approvals.sqlite3"),
        ),
    ):
        action = normalize_tool_action(
            name="delete_record",
            arguments={},
            context=context,
            side_effect=SideEffectClass.SIDE_EFFECTING,
        )
        with pytest.raises(ApprovalRequiresThreadError) as raised:
            guard_tool_call(
                action,
                context,
                lambda: calls.append("ran"),
                client=SequencedClient(),
                approval_lifecycle=lifecycle,
                interrupt=Pause(),
            )
        assert raised.value.code == "zeroth.approval_requires_thread"
    assert calls == []
