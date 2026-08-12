"""Per-run cumulative cost ceiling (WS-A gap #3).

The orchestrator can enforce a local ``per_run_cap_usd`` that halts a run once
the cumulative ``cost_usd`` recorded on its own audit history crosses the cap.
It works with the budget enforcer (Regulus) disabled: cost is read from each
node's audit record via ``_sum_run_cost``. Semantics are post-hoc — the run
halts on the NEXT node after spend crosses the cap.
"""

from __future__ import annotations

from typing import Any

import pytest

from zeroth.contracts.graph import AgentNode, AgentNodeData, Edge, Graph
from zeroth.governance.audit import AuditRepository
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.runs import RunStatus


class _CostingRunner:
    """Minimal agent runner that reports a fixed per-node cost in its audit record."""

    def __init__(self, cost_usd: float) -> None:
        self._cost_usd = cost_usd
        self.call_count = 0
        # Present so the orchestrator's getattr-guarded injection is a no-op.
        self.memory_resolver = None
        self.budget_enforcer = None

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: Any = None,
        enforcement_context: Any = None,
    ) -> Any:
        self.call_count += 1

        class _Result:
            output_data = {"answer": "ok"}
            audit_record = {"cost_usd": self._cost_usd}

        return _Result()


def _linear_graph(node_ids: list[str]) -> Graph:
    """Build a linear chain of AgentNodes with unconditional edges."""
    nodes = [
        AgentNode(
            node_id=nid,
            graph_version_ref="cap-graph:v1",
            agent=AgentNodeData(instruction="test", model_provider="provider://test"),
        )
        for nid in node_ids
    ]
    edges = [
        Edge(edge_id=f"edge-{i}", source_node_id=src, target_node_id=dst)
        for i, (src, dst) in enumerate(zip(node_ids, node_ids[1:], strict=False))
    ]
    return Graph(
        graph_id="cap-graph",
        name="cap",
        version=1,
        entry_step=node_ids[0],
        nodes=nodes,
        edges=edges,
    )


@pytest.mark.asyncio
async def test_per_run_cap_halts_run_on_next_node(sqlite_db) -> None:
    """A run whose accumulated cost_usd crosses the cap halts on the next node.

    Three nodes each cost $0.02; the cap is $0.03. n1 and n2 run (cumulative
    $0.04 > cap), so the check before n3 trips and n3 never dispatches.
    """
    runners = {nid: _CostingRunner(0.02) for nid in ("n1", "n2", "n3")}
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
        agent_runners=runners,
        executable_unit_runner=None,
        # Regulus disabled: no budget_enforcer, cap enforced locally.
        budget_enforcer=None,
        per_run_cap_usd=0.03,
    )

    run = await orchestrator.run_graph(_linear_graph(["n1", "n2", "n3"]), {"value": "go"})

    assert run.status is RunStatus.FAILED
    # n1 and n2 dispatched; n3 was halted before dispatch.
    assert runners["n1"].call_count == 1
    assert runners["n2"].call_count == 1
    assert runners["n3"].call_count == 0
    assert [e.node_id for e in run.execution_history] == ["n1", "n2"]


@pytest.mark.asyncio
async def test_per_run_cap_none_is_noop(sqlite_db) -> None:
    """With per_run_cap_usd=None the run completes regardless of cost."""
    runners = {nid: _CostingRunner(1.0) for nid in ("n1", "n2", "n3")}
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
        agent_runners=runners,
        executable_unit_runner=None,
        per_run_cap_usd=None,
    )

    run = await orchestrator.run_graph(_linear_graph(["n1", "n2", "n3"]), {"value": "go"})

    assert run.status is RunStatus.COMPLETED
    assert all(r.call_count == 1 for r in runners.values())
    assert [e.node_id for e in run.execution_history] == ["n1", "n2", "n3"]
