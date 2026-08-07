#!/usr/bin/env python3
"""Run one self-contained LangGraph governance release proof."""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from zeroth.governance.audit import NodeAuditRecord
from zeroth.integrations.langgraph import (
    ApprovalDecision,
    ApprovalResolution,
    SideEffectClass,
    SQLiteApprovalRepository,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
    ToolGovernanceError,
    ZerothGovernanceCallbackHandler,
    govern_graph,
)
from zeroth.integrations.langgraph._tool_guard import guard_tool_call
from zeroth.integrations.langgraph._tool_normalize import normalize_tool_action

CONTEXT = ToolGovernanceContext(
    tenant_id="demo-tenant",
    principal_id="demo-operator",
    run_id="demo-run",
    thread_id="demo-thread",
    correlation_id="demo-correlation",
)
ACTION = normalize_tool_action(
    name="release_write",
    arguments={"record": 27},
    context=CONTEXT,
    side_effect=SideEffectClass.SIDE_EFFECTING,
    tool_call_id="demo-call",
)


@dataclass
class DecisionClient:
    decision: ToolDecision

    def decide(self, _action: Any, _context: Any) -> ToolDecision:
        return self.decision


@dataclass
class AuditSink:
    records: list[NodeAuditRecord] = field(default_factory=list)

    def submit(self, record: NodeAuditRecord) -> bool:
        self.records.append(record)
        return True


@dataclass
class Body:
    calls: int = 0

    def __call__(self) -> str:
        self.calls += 1
        return "executed"


class SuspendedError(RuntimeError):
    pass


@dataclass
class Pause:
    payload: dict[str, Any] | None = None

    def __call__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        raise SuspendedError


class OrderedStream:
    def stream(self, *_args: Any, **_kwargs: Any):
        yield from ({"sequence": 1}, {"sequence": 2}, {"sequence": 3})


def run_demo() -> dict[str, Any]:
    """Exercise the existing decision, durable approval, audit, span, and stream seams."""
    audit = AuditSink()
    allow_body, denied_body, approved_body = Body(), Body(), Body()
    allow = ToolDecision(ToolDecisionKind.ALLOW, "unknown_error")
    deny = ToolDecision(ToolDecisionKind.DENY, "policy_violation")
    approval = ToolDecision(
        ToolDecisionKind.REQUIRE_APPROVAL,
        "policy_violation",
        approval_ref="approval-demo",
    )

    guard_tool_call(ACTION, CONTEXT, allow_body, client=DecisionClient(allow), audit=audit)
    try:
        guard_tool_call(ACTION, CONTEXT, denied_body, client=DecisionClient(deny), audit=audit)
    except ToolGovernanceError:
        pass

    with tempfile.TemporaryDirectory() as directory:
        lifecycle = SQLiteApprovalRepository(Path(directory) / "approvals.sqlite3")
        pause = Pause()
        try:
            guard_tool_call(
                ACTION,
                CONTEXT,
                approved_body,
                client=DecisionClient(approval),
                interrupt=pause,
                approval_lifecycle=lifecycle,
                audit=audit,
            )
        except SuspendedError:
            pass
        before_resume = approved_body.calls
        assert pause.payload is not None
        lifecycle.ready("approval-demo", "checkpoint-demo", "interrupt-demo")
        resolution = ApprovalResolution("approval-demo", ApprovalDecision.APPROVE)
        lifecycle.decide(resolution)
        claimed = lifecycle.claim("approval-demo", owner="demo-worker")
        assert claimed.claim_token is not None
        delivery = {**resolution.to_payload(), "claim_token": claimed.claim_token}
        guard_tool_call(
            ACTION,
            CONTEXT,
            approved_body,
            client=DecisionClient(allow),
            interrupt=lambda _payload: delivery,
            approval_lifecycle=lifecycle,
            audit=audit,
        )
        approval_state = lifecycle.get("approval-demo").state.value

    handler = ZerothGovernanceCallbackHandler()
    root, child = uuid4(), uuid4()
    handler.on_chain_start({"name": "root"}, {}, run_id=root)
    handler.on_chain_start({"name": "child"}, {}, run_id=child, parent_run_id=root)
    handler.on_chain_end({}, run_id=child, parent_run_id=root)
    handler.on_chain_end({}, run_id=root)
    spans = handler.completed_spans
    causal_valid = len(spans) == 2 and next(span for span in spans if span.run_id == str(child)).parent_run_id == str(root)
    stream_order = [chunk["sequence"] for chunk in govern_graph(OrderedStream()).stream({})]

    return {
        "audit_decisions": [record.execution_metadata["decision"] for record in audit.records],
        "allow_body_executions": allow_body.calls,
        "denied_body_executions": denied_body.calls,
        "approved_body_executions_before_resume": before_resume,
        "approved_body_executions_after_resume": approved_body.calls,
        "approval_state": approval_state,
        "causal_ancestry_valid": causal_valid,
        "stream_ordering": stream_order,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    evidence = run_demo()
    if args.json:
        print(json.dumps(evidence, sort_keys=True))
    else:
        print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
