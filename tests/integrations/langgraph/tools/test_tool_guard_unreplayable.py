"""Regression cover for the tool-guard completion fence on unreplayable results.

Two defects introduced by the LangGraph safety-hardening commit (065ef5be):

* the durable ``complete()`` call ran outside the invoke try/except, so a tool
  result that is not JSON-serialisable (a ``datetime``, a Pydantic model --
  routine for LangChain tools) raised *after* the side effect executed, left the
  claim ``IN_FLIGHT``, and wedged every redelivery on
  ``DuplicateToolExecutionError`` forever while reporting the successful effect
  as a failure; and
* the approval-completion audit record dropped whole when the result could not
  be fingerprinted, erasing the only evidence an approved call executed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import pytest

from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.integrations.langgraph._action_lifecycle import (
    ActionExecutionState,
    SQLiteActionExecutionRepository,
)
from zeroth.integrations.langgraph._tool_errors import (
    DuplicateToolExecutionError,
    ToolGovernanceError,
)
from zeroth.integrations.langgraph._tool_guard import (
    _emit_decision_audit,
    _result_outcome,
    aguard_tool_call,
    guard_tool_call,
)
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
ALLOW = ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")
ACTOR = ActorIdentity(subject="principal-1", auth_method=AuthMethod.API_KEY, tenant_id="tenant-a")

# A result the fence cannot serialise for durable replay.
UNSTORABLE_RESULT = {"receipt": "charged:order-41", "at": datetime(2026, 1, 1, 0, 0, 0)}


def _action(tool_call_id: str) -> ToolAction:
    return normalize_tool_action(
        name="charge",
        arguments={"order_id": "order-41"},
        context=CONTEXT,
        contract_ref="contract:billing",
        side_effect=SideEffectClass.SIDE_EFFECTING,
        tool_call_id=tool_call_id,
    )


@dataclass
class _StubClient:
    verdict: ToolDecision = ALLOW

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        del action, context
        return self.verdict


@dataclass
class _Effect:
    calls: int = 0
    result: Any = None

    def __call__(self) -> Any:
        self.calls += 1
        return self.result


@dataclass
class _RecordingAudit:
    records: list[NodeAuditRecord] = field(default_factory=list)

    def submit(self, record: NodeAuditRecord) -> None:
        self.records.append(record)


# --- F18: an unfingerprintable result must not sink the record -----------------


def test_result_outcome_is_defensive_about_unfingerprintable_results() -> None:
    fine = _result_outcome({"receipt": "ok"})
    assert isinstance(fine["result_fingerprint"], str) and fine["result_fingerprint"]

    degraded = _result_outcome(UNSTORABLE_RESULT)
    assert degraded == {"result_fingerprint": None, "unfingerprintable": True}


def test_completion_audit_survives_an_unfingerprintable_result() -> None:
    # Before the fix, result_fingerprint raised inside the projection and
    # _emit_decision_audit swallowed it, submitting zero records -- the approved
    # call's execution evidence vanished. It must now still land, flagged.
    audit = _RecordingAudit()

    _emit_decision_audit(
        audit,
        _action("call-audit-41"),
        CONTEXT,
        ALLOW,
        actor=ACTOR,
        approval_ref="approval-7",
        decision_term="approve",
        approval_action="approve",
        result=UNSTORABLE_RESULT,
        result_observed=True,
    )

    [record] = audit.records
    [tool_call] = record.tool_calls
    assert tool_call.outcome == {"result_fingerprint": None, "unfingerprintable": True}


# --- F17: the completion fence retires an unreplayable effect terminally --------


def test_sync_fence_retires_an_unreplayable_result_without_wedging(tmp_path: Any) -> None:
    lifecycle = SQLiteActionExecutionRepository(tmp_path / "actions.sqlite3")
    effect = _Effect(result=UNSTORABLE_RESULT)
    action = _action("call-charge-41")

    with pytest.raises(ToolGovernanceError, match="not durably replayable"):
        guard_tool_call(
            action,
            CONTEXT,
            effect,
            client=_StubClient(),
            action_lifecycle=lifecycle,
        )

    # The side effect ran exactly once, and the claim is terminal -- not the
    # IN_FLIGHT/AMBIGUOUS wedge the raw storage error used to leave behind.
    assert effect.calls == 1
    [record] = lifecycle.records()
    assert record.state is ActionExecutionState.UNREPLAYABLE


def test_a_redelivered_unreplayable_action_never_re_executes(tmp_path: Any) -> None:
    lifecycle = SQLiteActionExecutionRepository(tmp_path / "actions.sqlite3")
    effect = _Effect(result=UNSTORABLE_RESULT)
    action = _action("call-charge-41")

    with pytest.raises(ToolGovernanceError, match="not durably replayable"):
        guard_tool_call(
            action, CONTEXT, effect, client=_StubClient(), action_lifecycle=lifecycle
        )

    # A second delivery of the same identity is refused with a clear terminal
    # error -- never DuplicateToolExecutionError forever, and never a re-run of
    # the business effect.
    with pytest.raises(ToolGovernanceError) as raised:
        guard_tool_call(
            action, CONTEXT, _Effect(result=UNSTORABLE_RESULT), client=_StubClient(),
            action_lifecycle=lifecycle,
        )
    assert not isinstance(raised.value, DuplicateToolExecutionError)
    assert "already executed" in str(raised.value)
    assert effect.calls == 1


async def test_async_fence_retires_an_unreplayable_result_without_wedging(tmp_path: Any) -> None:
    lifecycle = SQLiteActionExecutionRepository(tmp_path / "actions.sqlite3")
    action = replace(_action("call-charge-async-41"))
    calls = 0

    async def effect() -> Any:
        nonlocal calls
        calls += 1
        return UNSTORABLE_RESULT

    with pytest.raises(ToolGovernanceError, match="not durably replayable"):
        await aguard_tool_call(
            action,
            CONTEXT,
            effect,
            client=_StubClient(),
            action_lifecycle=lifecycle,
        )

    assert calls == 1
    [record] = lifecycle.records()
    assert record.state is ActionExecutionState.UNREPLAYABLE
