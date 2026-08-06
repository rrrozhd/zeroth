"""Proof that the shared enforcement core allows once, denies never, and pauses cold.

The three outcomes are asserted the only way that means anything: with a counter
on the downstream invocation. "Denied" is not "raised" -- it is "raised *and* the
tool ran zero times" -- and an implementation that invoked first and raised
afterwards would satisfy every exception assertion in a suite that did not count.
So every enforcement test below reads ``downstream.calls``.

The approval branch is exercised with an injected fake ``interrupt``. That is why
the core takes it as a parameter: an import performed inside the enforcement
function would leave no seam to substitute, and a test that patched nothing would
assert nothing while passing. With the seam injected, none of this needs
``langgraph`` or ``langchain``, so nothing here carries the
``langgraph_conformance`` marker -- ``addopts`` deselects it and a marked test
would never run.

The audit assertions go through storage, not through the projection. Asserting
that ``require_approval`` is a member of the vocabulary frozenset would prove
nothing about retention: the ZER-5 failure shape is a record that writes
successfully while the field that says why is replaced by a digest. Only a write
and a read back distinguishes the two.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import tempfile
import threading
from enum import StrEnum
from typing import Any

import pytest

from tests.integrations.langgraph.tools._hostile import HostileStr
from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.governance.audit.delivery import AuditDeliveryQueue
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.langgraph import _tool_guard
from zeroth.integrations.langgraph._approval_lifecycle import SQLiteApprovalRepository
from zeroth.integrations.langgraph._tool_decisions import UnknownSideEffectPolicy
from zeroth.integrations.langgraph._tool_errors import (
    ApprovalRequiresThreadError,
    GovernanceContextError,
    PolicyViolation,
    ToolGovernanceError,
    UnstableToolIdentityError,
)
from zeroth.integrations.langgraph._tool_guard import (
    TOOL_GUARD_NODE_ID,
    _recognized_decision,
    aguard_tool_call,
    authorize_tool_call,
    guard_tool_call,
)
from zeroth.integrations.langgraph._tool_normalize import normalize_tool_action
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
    ToolIdentity,
)

THREADED = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-1",
    run_id="run-1",
    thread_id="thread-1",
    correlation_id="corr-1",
)
"""A run that can be paused: approval needs a thread to resume into."""

THREADLESS = ToolGovernanceContext(tenant_id="tenant-a", principal_id="principal-1", run_id="run-1")

ACTION = normalize_tool_action(
    name="delete_record",
    arguments={"table": "invoices", "id": 41},
    context=THREADED,
    contract_ref="contract:records",
    side_effect=SideEffectClass.SIDE_EFFECTING,
)

ALLOW = ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")
DENY = ToolDecision(kind=ToolDecisionKind.DENY, reason_code="policy_violation")
APPROVE = ToolDecision(
    kind=ToolDecisionKind.REQUIRE_APPROVAL,
    reason_code="policy_violation",
    approval_ref="approval-7",
)

ACTOR = ActorIdentity(subject="principal-1", auth_method=AuthMethod.API_KEY, tenant_id="tenant-a")


class OtherKind(StrEnum):
    """A verdict enum that is not ``ToolDecisionKind`` and spells ``allow`` the same."""

    ALLOW = "allow"


@dataclasses.dataclass(frozen=True, slots=True)
class DecisionSubclass(ToolDecision):
    """A subclass ``isinstance`` accepts and the exact-type gate does not."""


@dataclasses.dataclass
class StubClient:
    """A client that returns whatever it was handed."""

    verdict: object = ALLOW

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Return the configured verdict."""
        return self.verdict  # type: ignore[return-value]


@dataclasses.dataclass
class BlockingClient:
    """A decision client held until the event loop proves it is still live."""

    started: threading.Event = dataclasses.field(default_factory=threading.Event)
    release: threading.Event = dataclasses.field(default_factory=threading.Event)
    thread_id: int | None = None
    released: bool = False

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Wait boundedly for the test coroutine to release authorization."""
        del action, context
        self.thread_id = threading.get_ident()
        self.started.set()
        self.released = self.release.wait(timeout=2)
        return ALLOW


class TimeoutClient:
    """A decision client whose policy source times out."""

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Raise the transport-shaped failure the shared seam must deny."""
        del action, context
        raise TimeoutError("decision timed out")


@dataclasses.dataclass
class Downstream:
    """The tool body, which counts every time it is invoked."""

    calls: int = 0
    result: object = "tool-result"
    error: BaseException | None = None

    def __call__(self) -> object:
        """Record the invocation and either fail or return."""
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@dataclasses.dataclass
class FakeInterrupt:
    """A pause seam that records its payload instead of suspending the run."""

    calls: int = 0
    payload: Any = None

    def __call__(self, payload: Any) -> None:
        """Record one suspension request."""
        self.calls += 1
        self.payload = payload


@dataclasses.dataclass
class RecordingSubmitter:
    """An audit hand-off that keeps what it was given."""

    records: list[NodeAuditRecord] = dataclasses.field(default_factory=list)

    def submit(self, record: NodeAuditRecord) -> bool:
        """Keep the record and report it queued."""
        self.records.append(record)
        return True


@dataclasses.dataclass
class RaisingSubmitter:
    """An audit hand-off that fails the way an injected one eventually will."""

    def submit(self, record: NodeAuditRecord) -> bool:
        """Fail instead of queueing."""
        raise RuntimeError("delivery stage is gone")


def guard(**overrides: Any) -> Any:
    """Run the entry point against the side-effecting action, with fields overridden."""
    kwargs: dict[str, Any] = {"action": ACTION, "context": THREADED}
    kwargs.update(overrides)
    with tempfile.TemporaryDirectory() as directory:
        kwargs.setdefault(
            "approval_lifecycle", SQLiteApprovalRepository(f"{directory}/approvals.sqlite3")
        )
        return guard_tool_call(**kwargs)


# --- R4: an allow invokes the downstream tool exactly once ---------------------


def test_an_allowed_call_invokes_the_downstream_tool_exactly_once() -> None:
    downstream = Downstream()

    result = guard(invoke=downstream, client=StubClient(ALLOW))

    assert downstream.calls == 1
    assert result == "tool-result"


def test_a_downstream_failure_propagates_unchanged_and_is_never_retried() -> None:
    # A counter-only test cannot tell "invoked once" from "invoked, failed,
    # invoked again": governance decides whether a call happens, not what it
    # means, so the exception has to come back untouched and the count stay at 1.
    failure = RuntimeError("the tool itself broke")
    downstream = Downstream(error=failure)

    with pytest.raises(RuntimeError) as raised:
        guard(invoke=downstream, client=StubClient(ALLOW))

    assert raised.value is failure
    assert downstream.calls == 1


def test_authorizing_without_invoking_returns_the_allow_verdict() -> None:
    verdict = authorize_tool_call(ACTION, THREADED, client=StubClient(ALLOW))

    assert verdict.kind is ToolDecisionKind.ALLOW


async def test_async_authorization_keeps_the_event_loop_live_and_invokes_once() -> None:
    client = BlockingClient()
    event_loop_thread = threading.get_ident()
    downstream_calls = 0

    async def heartbeat() -> bool:
        return await asyncio.to_thread(client.started.wait, 2)

    async def downstream() -> str:
        nonlocal downstream_calls
        downstream_calls += 1
        return "tool-result"

    guarded = asyncio.create_task(aguard_tool_call(ACTION, THREADED, downstream, client=client))
    try:
        authorization_started = await heartbeat()
        heartbeat_beat_before_release = not guarded.done()
    finally:
        client.release.set()

    result = await asyncio.wait_for(guarded, 2)

    assert authorization_started
    assert heartbeat_beat_before_release
    assert client.released
    assert client.thread_id != event_loop_thread
    assert downstream_calls == 1
    assert result == "tool-result"


async def test_async_policy_timeout_delivers_the_outage_on_the_event_loop(sqlite_db) -> None:
    repository = AuditRepository(sqlite_db)
    audit = AuditDeliveryQueue(repository, base_delay_seconds=0, max_delay_seconds=0)
    downstream_calls = 0

    async def downstream() -> None:
        nonlocal downstream_calls
        downstream_calls += 1

    try:
        with pytest.raises(PolicyViolation):
            await aguard_tool_call(
                ACTION,
                THREADED,
                downstream,
                client=TimeoutClient(),
                audit=audit,
                actor=ACTOR,
            )
    finally:
        report = await audit.aclose(timeout=2)

    stored = await repository.list_by_run("run-1")
    assert downstream_calls == 0
    assert report.drained
    assert report.undelivered_audit_ids == ()
    assert report.counts.queued == 1
    assert report.counts.delivered == 1
    assert audit.pending == 0
    assert len(stored) == 1
    assert stored[0].execution_metadata["decision"] == "deny"
    assert stored[0].execution_metadata["reason_code"] == "policy_unavailable"


# --- R5: a denial raises before the body, which runs zero times ----------------


def test_a_denied_call_raises_a_policy_violation_and_never_invokes_the_tool() -> None:
    downstream = Downstream()

    with pytest.raises(PolicyViolation):
        guard(invoke=downstream, client=StubClient(DENY))

    assert downstream.calls == 0


def test_a_call_with_no_decision_client_is_denied_rather_than_waved_through() -> None:
    downstream = Downstream()

    with pytest.raises(PolicyViolation):
        guard(invoke=downstream)

    assert downstream.calls == 0


def test_an_unclassified_tool_is_denied_unless_the_named_opt_in_is_in_force() -> None:
    unclassified = normalize_tool_action(
        name="mystery", arguments={}, context=THREADED, side_effect=None
    )
    downstream = Downstream()

    with pytest.raises(PolicyViolation):
        guard_tool_call(unclassified, THREADED, downstream, client=StubClient(ALLOW))

    assert downstream.calls == 0
    assert (
        guard_tool_call(
            unclassified,
            THREADED,
            Downstream(),
            client=StubClient(ALLOW),
            unknown_side_effect=UnknownSideEffectPolicy.ALLOW_UNCLASSIFIED_TOOLS,
        )
        == "tool-result"
    )


@pytest.mark.parametrize(
    "verdict",
    [
        pytest.param(DecisionSubclass(kind=ToolDecisionKind.ALLOW, reason_code="x"), id="subclass"),
        pytest.param(ToolDecision(kind=OtherKind.ALLOW, reason_code="x"), id="foreign-enum"),
        pytest.param(None, id="none"),
        pytest.param(HostileStr("allow"), id="hostile-str"),
    ],
)
def test_no_impostor_verdict_can_slip_an_allow_through_the_guard(verdict: object) -> None:
    downstream = Downstream()

    with pytest.raises(PolicyViolation):
        guard(invoke=downstream, client=StubClient(verdict))

    assert downstream.calls == 0


@pytest.mark.parametrize(
    "verdict",
    [
        pytest.param(DecisionSubclass(kind=ToolDecisionKind.ALLOW, reason_code="x"), id="subclass"),
        pytest.param(ToolDecision(kind=OtherKind.ALLOW, reason_code="x"), id="foreign-enum"),
        pytest.param(None, id="none"),
    ],
)
def test_the_guards_own_verdict_gate_refuses_an_impostor_reaching_it_directly(
    verdict: object,
) -> None:
    # The composed path above goes through ``resolve_tool_decision``, which
    # rebuilds every verdict from gated fields -- so it proves T3's gate, not
    # this one. Calling the guard's gate directly is what keeps the
    # defence-in-depth line covered rather than dead.
    with pytest.raises(PolicyViolation):
        _recognized_decision(verdict)


# --- R6: an approval interrupts before the body, which runs zero times ---------


def test_an_approval_interrupts_with_a_payload_and_never_invokes_the_tool() -> None:
    downstream = Downstream()
    interrupt = FakeInterrupt()

    with pytest.raises(ToolGovernanceError):
        guard(invoke=downstream, client=StubClient(APPROVE), interrupt=interrupt)

    assert interrupt.calls == 1
    assert downstream.calls == 0


def test_an_interrupt_that_returns_refuses_the_call_until_zer10_revalidates_on_resume() -> None:
    # ``interrupt`` normally suspends by raising, so this branch is unreachable in
    # production today. When ZER-10 lands resume-time revalidation it becomes
    # reachable and this expectation is the thing to change -- named so ZER-10
    # finds it. Until then, falling through would invoke the tool with the
    # approval unanswered.
    downstream = Downstream()

    with pytest.raises(ToolGovernanceError) as raised:
        guard(invoke=downstream, client=StubClient(APPROVE), interrupt=FakeInterrupt())

    assert type(raised.value) is ToolGovernanceError
    assert downstream.calls == 0


def test_the_approval_payload_carries_a_version_and_the_approval_reference() -> None:
    interrupt = FakeInterrupt()

    with pytest.raises(ToolGovernanceError):
        guard(invoke=Downstream(), client=StubClient(APPROVE), interrupt=interrupt)

    payload = interrupt.payload
    assert payload["version"] == 1
    assert payload["kind"] == "tool_approval"
    assert payload["approval_ref"] == "approval-7"
    assert payload["tool_name"] == "delete_record"
    assert payload["tool_fingerprint"] == ACTION.identity.fingerprint
    assert payload["thread_id"] == "thread-1"
    assert payload["side_effect"] == "side_effecting"


def test_the_approval_payload_survives_a_json_round_trip_unchanged() -> None:
    # The payload is written into graph state that outlives the run, so anything
    # that does not serialize is a pause that cannot be checkpointed. Equality
    # alone is not enough: a ``StrEnum`` member compares equal to its own string
    # and would round-trip "unchanged" while being an enum on the way in, so the
    # types are asserted too.
    interrupt = FakeInterrupt()
    with pytest.raises(ToolGovernanceError):
        guard(invoke=Downstream(), client=StubClient(APPROVE), interrupt=interrupt)

    payload = interrupt.payload

    assert json.loads(json.dumps(payload)) == payload
    for key, value in payload.items():
        assert type(key) is str
        assert type(value) in (str, int, type(None)), f"{key} is {type(value).__name__}"


def test_an_approval_on_a_threadless_run_fails_closed_instead_of_becoming_an_allow() -> None:
    # The rule the whole branch exists for: a pause nobody can resume must not
    # quietly turn into a pass. The interrupt is never even reached.
    threadless_action = normalize_tool_action(
        name="delete_record",
        arguments={"table": "invoices"},
        context=THREADLESS,
        side_effect=SideEffectClass.SIDE_EFFECTING,
    )
    downstream = Downstream()
    interrupt = FakeInterrupt()

    with pytest.raises(ApprovalRequiresThreadError) as raised:
        guard_tool_call(
            threadless_action,
            THREADLESS,
            downstream,
            client=StubClient(APPROVE),
            interrupt=interrupt,
        )

    assert raised.value.code == "zeroth.approval_requires_thread"
    assert interrupt.calls == 0
    assert downstream.calls == 0


def test_an_approval_with_no_reference_is_refused_rather_than_requested() -> None:
    unanswerable = ToolDecision(kind=ToolDecisionKind.REQUIRE_APPROVAL, reason_code="unknown_error")
    downstream = Downstream()
    interrupt = FakeInterrupt()

    with pytest.raises(ToolGovernanceError):
        guard(invoke=downstream, client=StubClient(unanswerable), interrupt=interrupt)

    assert interrupt.calls == 0
    assert downstream.calls == 0


def test_omitting_the_interrupt_selects_the_module_default_and_pauses_through_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The property that matters: with no ``interrupt`` argument the approval
    # branch *selects* the module-level default. Inspecting
    # ``_langgraph_interrupt``'s bytecode cannot show that -- it passes even if
    # nothing ever calls the function -- so the default is substituted at the
    # module attribute ``_suspend_for_approval`` reads at call time, and the
    # substitute is asserted to have received the real payload before a
    # zero-count downstream. Still Tier B: patching the seam is exactly why the
    # parameter exists, so no ``langgraph`` is installed or imported here.
    seam = FakeInterrupt()
    monkeypatch.setattr(_tool_guard, "_langgraph_interrupt", seam)
    downstream = Downstream()

    # The substitute returns rather than suspending, so the guard refuses the
    # call on the fall-through -- which is itself the proof it went through the
    # seam and did not sail past it.
    with pytest.raises(ToolGovernanceError):
        guard(invoke=downstream, client=StubClient(APPROVE))

    assert seam.calls == 1
    assert seam.payload["kind"] == "tool_approval"
    assert seam.payload["approval_ref"] == "approval-7"
    assert seam.payload["tool_name"] == "delete_record"
    assert downstream.calls == 0


def test_the_default_pause_seam_resolves_langgraphs_own_interrupt() -> None:
    # The second half of the same property, and a genuinely different claim from
    # the test above: that one proves the approval branch reaches the module
    # default, this one proves the module default is LangGraph's ``interrupt``
    # rather than some other callable. Read off the bytecode because calling it
    # for real would need ``langgraph`` installed, which is the dependency this
    # tier does not have.
    source = _tool_guard._langgraph_interrupt.__code__.co_names

    assert "interrupt" in source


# --- fail-closed identity and attribution --------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        pytest.param({"identity": "delete_record"}, id="not-a-tool-action"),
        pytest.param(
            ToolAction(identity=ToolIdentity(name="t", fingerprint="   "), arguments={}),
            id="blank-fingerprint",
        ),
        pytest.param(
            ToolAction(identity=ToolIdentity(name="", fingerprint="abc"), arguments={}),
            id="blank-name",
        ),
        pytest.param(
            ToolAction(identity=("delete_record", "abc"), arguments={}),  # type: ignore[arg-type]
            id="identity-is-a-tuple",
        ),
    ],
)
def test_a_tool_without_a_stable_identity_is_refused_before_any_decision(action: object) -> None:
    downstream = Downstream()

    with pytest.raises(UnstableToolIdentityError):
        guard_tool_call(action, THREADED, downstream, client=StubClient(ALLOW))

    assert downstream.calls == 0


def test_an_action_normalized_under_one_principal_cannot_be_enforced_under_another() -> None:
    # Not attacker-reachable today: nothing ties normalization and enforcement to
    # one context except this check. Without it a decision recorded against
    # principal B describes a call attributed to principal A, which is
    # attribution an auditor cannot unpick afterwards.
    other = ToolGovernanceContext(
        tenant_id="tenant-a", principal_id="principal-2", run_id="run-1", thread_id="thread-1"
    )
    downstream = Downstream()

    with pytest.raises(GovernanceContextError):
        guard_tool_call(ACTION, other, downstream, client=StubClient(ALLOW))

    assert downstream.calls == 0


def test_an_unattributable_call_is_undecidable_and_records_nothing() -> None:
    audit = RecordingSubmitter()
    downstream = Downstream()

    with pytest.raises(GovernanceContextError):
        guard_tool_call(ACTION, None, downstream, client=StubClient(ALLOW), audit=audit)

    assert audit.records == []
    assert downstream.calls == 0


def test_a_principal_mismatch_records_nothing_because_there_is_nobody_to_record_it_against() -> (
    None
):
    other = ToolGovernanceContext(
        tenant_id="tenant-a", principal_id="principal-2", run_id="run-1", thread_id="thread-1"
    )
    audit = RecordingSubmitter()

    with pytest.raises(GovernanceContextError):
        guard_tool_call(ACTION, other, Downstream(), client=StubClient(ALLOW), audit=audit)

    assert audit.records == []


# --- audit: the typed tool_calls field, never a new metadata key ---------------


def _submitted(verdict: object, **overrides: Any) -> NodeAuditRecord:
    """Run the guard to completion or failure and return the one record submitted."""
    audit = RecordingSubmitter()
    kwargs: dict[str, Any] = {"client": StubClient(verdict), "audit": audit, "actor": ACTOR}
    kwargs.update(overrides)
    with contextlib.suppress(ToolGovernanceError):
        guard(invoke=Downstream(), **kwargs)
    assert len(audit.records) == 1
    return audit.records[0]


def test_an_allow_is_recorded_as_a_decision_and_not_as_a_failure() -> None:
    record = _submitted(ALLOW)

    assert record.execution_metadata == {"decision": "allow"}
    assert record.status == "completed"
    assert record.node_id == TOOL_GUARD_NODE_ID


def test_a_denial_records_the_verdict_and_the_reason_it_was_refused_under() -> None:
    record = _submitted(DENY)

    assert record.execution_metadata == {"decision": "deny", "reason_code": "policy_violation"}
    assert record.status == "rejected"
    assert record.tool_calls[0].error == "policy_violation"


def test_an_approval_records_the_pending_verdict_and_the_approval_it_waits_on() -> None:
    record = _submitted(APPROVE, interrupt=FakeInterrupt())

    assert record.execution_metadata == {"decision": "require_approval"}
    assert record.status == "waiting_approval"
    assert [action.approval_id for action in record.approval_actions] == ["approval-7"]
    assert record.approval_actions[0].action == "requested"


def test_the_tool_travels_the_typed_tool_calls_field_with_no_outcome() -> None:
    # ``execution_metadata`` has no key for a tool name or reference, and an
    # unrecognized key is dropped silently -- so the evidence has to ride the
    # typed field. ``outcome`` stays absent because the record is written before
    # the tool runs and the core never observes what it returned.
    record = _submitted(ALLOW)

    assert len(record.tool_calls) == 1
    call = record.tool_calls[0]
    assert call.alias == "delete_record"
    assert call.tool_ref == ACTION.identity.fingerprint
    assert call.arguments == {"table": "invoices", "id": 41}
    assert call.outcome is None


def test_every_recorded_metadata_value_is_a_plain_str_not_a_str_enum() -> None:
    # The capture projection gates a vocabulary value with ``type(value) is str``.
    # A ``StrEnum`` member passes a membership test and is then summarized away
    # at write time, so the record would look fine here and lose the decision in
    # storage.
    for verdict, extra in ((ALLOW, {}), (DENY, {}), (APPROVE, {"interrupt": FakeInterrupt()})):
        record = _submitted(verdict, **extra)
        for key, value in record.execution_metadata.items():
            assert type(key) is str
            assert type(value) is str, f"{key} is {type(value).__name__}"


@pytest.mark.parametrize(
    ("verdict", "expected", "extra"),
    [
        pytest.param(ALLOW, "allow", {}, id="allow"),
        pytest.param(DENY, "deny", {}, id="deny"),
        pytest.param(APPROVE, "require_approval", {"interrupt": FakeInterrupt()}, id="approval"),
    ],
)
async def test_each_recorded_verdict_survives_the_write_into_storage(
    sqlite_db, verdict: object, expected: str, extra: dict[str, Any]
) -> None:
    # The assertion that matters. Membership in the vocabulary frozenset proves
    # nothing: an unretained term still writes a record and still reports a
    # decision, with the field replaced by a digest/schema/count summary. Only
    # reading it back distinguishes the two.
    repository = AuditRepository(sqlite_db)
    record = _submitted(verdict, **extra)

    await repository.write(record)
    stored = await repository.list_by_run("run-1")

    match = [row for row in stored if row.audit_id == record.audit_id]
    assert len(match) == 1
    assert match[0].execution_metadata["decision"] == expected


async def test_the_tool_reference_and_approval_id_survive_the_write_into_storage(
    sqlite_db,
) -> None:
    repository = AuditRepository(sqlite_db)
    record = _submitted(APPROVE, interrupt=FakeInterrupt())

    await repository.write(record)
    stored = await repository.list_by_run("run-1")

    match = [row for row in stored if row.audit_id == record.audit_id][0]
    assert match.tool_calls[0].alias == "delete_record"
    assert match.tool_calls[0].tool_ref == ACTION.identity.fingerprint
    assert match.approval_actions[0].approval_id == "approval-7"


# --- the audit path never decides whether a call happens -----------------------


def test_a_submitter_that_raises_does_not_turn_an_allow_into_a_failure() -> None:
    downstream = Downstream()

    result = guard(invoke=downstream, client=StubClient(ALLOW), audit=RaisingSubmitter())

    assert downstream.calls == 1
    assert result == "tool-result"


def test_a_submitter_that_raises_does_not_turn_a_denial_into_an_allow() -> None:
    downstream = Downstream()

    with pytest.raises(PolicyViolation):
        guard(invoke=downstream, client=StubClient(DENY), audit=RaisingSubmitter())

    assert downstream.calls == 0


def test_a_projection_that_will_not_validate_costs_one_record_and_not_the_call() -> None:
    # An actor the record model refuses fails the projection before the submit,
    # which is the other half of "never raise from the audit path".
    audit = RecordingSubmitter()
    downstream = Downstream()

    result = guard(
        invoke=downstream,
        client=StubClient(ALLOW),
        audit=audit,
        actor=object(),  # type: ignore[arg-type]
    )

    assert audit.records == []
    assert downstream.calls == 1
    assert result == "tool-result"


def test_deciding_without_an_audit_submitter_still_enforces() -> None:
    downstream = Downstream()

    with pytest.raises(PolicyViolation):
        guard(invoke=downstream, client=StubClient(DENY), audit=None)

    assert downstream.calls == 0
