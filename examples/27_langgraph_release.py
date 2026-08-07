#!/usr/bin/env python3
"""Run one self-contained LangGraph governance release proof."""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from operator import add
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from zeroth.governance.audit import NodeAuditRecord
from zeroth.integrations.langgraph import (
    ApprovalCoordinator,
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
    govern_tools,
)

THREAD_ID = "demo-thread"
CONTEXT = ToolGovernanceContext(
    tenant_id="demo-tenant",
    principal_id="demo-operator",
    run_id="demo-run",
    thread_id=THREAD_ID,
    correlation_id="demo-correlation",
)


class DemoState(TypedDict):
    sequence: Annotated[list[int], add]


@dataclass
class DecisionClient:
    decisions: list[ToolDecision]

    def decide(self, _action: Any, _context: Any) -> ToolDecision:
        return self.decisions.pop(0)


@dataclass
class AuditSink:
    records: list[NodeAuditRecord] = field(default_factory=list)

    def submit(self, record: NodeAuditRecord) -> bool:
        self.records.append(record)
        return True


def _govern(tool: Any, client: DecisionClient, audit: AuditSink, repository: Any = None) -> Any:
    [governed] = govern_tools(
        [tool],
        context=CONTEXT,
        client=client,
        audit=audit,
        approval_lifecycle=repository,
        side_effect=lambda _tool: SideEffectClass.SIDE_EFFECTING,
    )
    return governed


def _single_node_graph(node: Any, checkpointer: Any = None) -> Any:
    builder = StateGraph(DemoState)
    builder.add_node("governed_tool", node)
    builder.add_edge(START, "governed_tool")
    builder.add_edge("governed_tool", END)
    return govern_graph(builder.compile(checkpointer=checkpointer))


def run_demo() -> dict[str, Any]:
    """Exercise public governance on real, streamed, durably resumed StateGraphs."""
    audit = AuditSink()
    executions = {"allow": 0, "deny": 0, "approval": 0}

    def allowed_write() -> None:
        executions["allow"] += 1

    allowed = _govern(
        allowed_write,
        DecisionClient([ToolDecision(ToolDecisionKind.ALLOW, "unknown_error")]),
        audit,
    )

    def allow_node(_state: DemoState) -> DemoState:
        allowed()
        return {"sequence": [1]}

    def second_node(_state: DemoState) -> DemoState:
        return {"sequence": [2]}

    def third_node(_state: DemoState) -> DemoState:
        return {"sequence": [3]}

    stream_builder = StateGraph(DemoState)
    stream_builder.add_node("allow", allow_node)
    stream_builder.add_node("second", second_node)
    stream_builder.add_node("third", third_node)
    stream_builder.add_edge(START, "allow")
    stream_builder.add_edge("allow", "second")
    stream_builder.add_edge("second", "third")
    stream_builder.add_edge("third", END)
    handler = ZerothGovernanceCallbackHandler()
    stream_config = {
        "callbacks": [handler],
        "configurable": {"thread_id": "demo-stream-thread"},
    }
    chunks = list(
        govern_graph(stream_builder.compile()).stream(
            {"sequence": []}, stream_config, stream_mode="updates"
        )
    )
    stream_order = [next(iter(chunk.values()))["sequence"][0] for chunk in chunks]
    span_ids = {span.run_id for span in handler.completed_spans}
    causal_valid = any(
        span.parent_run_id in span_ids
        for span in handler.completed_spans
        if span.parent_run_id is not None
    )

    def denied_write() -> None:
        executions["deny"] += 1

    denied = _govern(
        denied_write,
        DecisionClient([ToolDecision(ToolDecisionKind.DENY, "policy_violation")]),
        audit,
    )

    def deny_node(_state: DemoState) -> DemoState:
        denied()
        return {"sequence": []}

    try:
        _single_node_graph(deny_node).invoke(
            {"sequence": []}, {"configurable": {"thread_id": "demo-deny-thread"}}
        )
    except ToolGovernanceError:
        pass

    with tempfile.TemporaryDirectory() as directory:
        repository = SQLiteApprovalRepository(Path(directory) / "approvals.sqlite3")

        def approved_write() -> None:
            executions["approval"] += 1

        approved = _govern(
            approved_write,
            DecisionClient(
                [
                    ToolDecision(
                        ToolDecisionKind.REQUIRE_APPROVAL,
                        "policy_violation",
                        approval_ref="approval-demo",
                    ),
                    ToolDecision(ToolDecisionKind.ALLOW, "unknown_error"),
                ]
            ),
            audit,
            repository,
        )

        def approval_node(_state: DemoState) -> DemoState:
            approved()
            return {"sequence": []}

        checkpoint_path = Path(directory) / "checkpoints.sqlite3"
        with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            graph = _single_node_graph(approval_node, saver)
            config = {"configurable": {"thread_id": THREAD_ID}}
            list(graph.stream({"sequence": []}, config, stream_mode="updates"))
            before_resume = executions["approval"]
            coordinator = ApprovalCoordinator(repository)
            coordinator.confirm_checkpoint(
                "approval-demo", graph, config=config, durable_checkpointer=saver
            )
            repository.decide(
                ApprovalResolution("approval-demo", ApprovalDecision.APPROVE)
            )
            coordinator.resume(
                "approval-demo",
                graph,
                owner="demo-worker",
                config=config,
                durable_checkpointer=saver,
            )
            approval_state = repository.get("approval-demo").state.value

    return {
        "audit_decisions": [record.execution_metadata["decision"] for record in audit.records],
        "allow_body_executions": executions["allow"],
        "denied_body_executions": executions["deny"],
        "approved_body_executions_before_resume": before_resume,
        "approved_body_executions_after_resume": executions["approval"],
        "approval_state": approval_state,
        "causal_ancestry_valid": causal_valid,
        "stream_ordering": stream_order,
        "thread_id": THREAD_ID,
        "checkpointer": "SqliteSaver",
        "resume_api": "Command(resume=...)",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    evidence = run_demo()
    print(json.dumps(evidence, sort_keys=True) if args.json else json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
