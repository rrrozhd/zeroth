"""Integration tests for per-branch governance, budget, policy, and step limits.

Tests that the parallel fan-out engine enforces governance rules independently
per branch: each branch gets its own audit record with branch_id, policy is
evaluated per branch, budget is checked before spawning, and the global step
tracker enforces max_total_steps as the sum across all branches.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime import AgentConfig, AgentRunner
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.core.audit import AuditRepository
from zeroth.core.execution_units import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.parallel.models import ParallelConfig
from zeroth.core.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDecision,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.core.policy.models import EnforcementResult
from zeroth.core.runs import RunRepository, RunStatus


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------

class SourceInput(BaseModel):
    value: int = 0


class BranchItemInput(BaseModel):
    x: int = 0


class ItemsOutput(BaseModel):
    items: list[dict[str, Any]] = []


class ProcessedOutput(BaseModel):
    result: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent_runner(
    *,
    output_model: type[BaseModel],
    handler,
    input_model: type[BaseModel] = SourceInput,
) -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name="test-agent",
            instruction="test",
            model_name="governai:test",
            input_model=input_model,
            output_model=output_model,
        ),
        CallableProviderAdapter(handler),
    )


def _make_agent_node(
    node_id: str,
    *,
    parallel_config: ParallelConfig | None = None,
) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref="test-gov:v1",
        agent=AgentNodeData(instruction="test", model_provider=f"provider://{node_id}"),
        parallel_config=parallel_config,
    )


def _make_graph(
    nodes: list,
    edges: list,
    *,
    entry_step: str = "source",
    max_total_steps: int = 50,
) -> Graph:
    return Graph(
        graph_id="test-gov",
        name="test-gov",
        entry_step=entry_step,
        execution_settings=ExecutionSettings(max_total_steps=max_total_steps),
        nodes=nodes,
        edges=edges,
    )


def _make_orchestrator(
    agent_runners: dict[str, AgentRunner],
    sqlite_db,
    *,
    audit_repository: AuditRepository | None = None,
    policy_guard: PolicyGuard | None = None,
    budget_enforcer: object | None = None,
) -> RuntimeOrchestrator:
    eu_registry = ExecutableUnitRegistry()
    return RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners=agent_runners,
        executable_unit_runner=ExecutableUnitRunner(eu_registry),
        audit_repository=audit_repository,
        policy_guard=policy_guard,
        budget_enforcer=budget_enforcer,
    )


def _source_runner():
    """Source runner that produces 3 items for fan-out."""
    return _make_agent_runner(
        output_model=ItemsOutput,
        handler=lambda req: ProviderResponse(
            content={"items": [{"x": 1}, {"x": 2}, {"x": 3}]}
        ),
    )


def _sink_runner():
    """Sink runner that processes a branch item."""
    return _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=lambda req: ProviderResponse(
            content={"result": req.metadata["input_payload"].get("x", 0) * 10}
        ),
    )


def _fan_out_graph(*, max_total_steps: int = 50) -> Graph:
    """Standard 2-node graph: source (fan-out) -> sink."""
    return _make_graph(
        [
            _make_agent_node("source", parallel_config=ParallelConfig(split_path="items")),
            _make_agent_node("sink"),
        ],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
        max_total_steps=max_total_steps,
    )


# ---------------------------------------------------------------------------
# Audit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_branch_audit_records(sqlite_db) -> None:
    """3-branch fan-out produces 3 separate NodeAuditRecords with branch_id."""
    audit_repo = AuditRepository(sqlite_db)
    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": _sink_runner()},
        sqlite_db,
        audit_repository=audit_repo,
    )

    run = await orchestrator.run_graph(_fan_out_graph(), {"value": 1})

    assert run.status is RunStatus.COMPLETED
    audits = await audit_repo.list_by_run(run.run_id)
    # Source audit + 3 branch audits = 4 total
    assert len(audits) >= 4

    # Filter branch audit records (those with branch_id in metadata)
    branch_audits = [a for a in audits if "branch_id" in a.execution_metadata]
    assert len(branch_audits) == 3
    # Each should have distinct branch_id
    branch_ids = {a.execution_metadata["branch_id"] for a in branch_audits}
    assert len(branch_ids) == 3
    # Each should have branch_index
    branch_indices = {a.execution_metadata["branch_index"] for a in branch_audits}
    assert branch_indices == {0, 1, 2}


@pytest.mark.asyncio
async def test_per_branch_audit_linked_to_parent(sqlite_db) -> None:
    """All branch audit records have same run_id as parent."""
    audit_repo = AuditRepository(sqlite_db)
    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": _sink_runner()},
        sqlite_db,
        audit_repository=audit_repo,
    )

    run = await orchestrator.run_graph(_fan_out_graph(), {"value": 1})

    audits = await audit_repo.list_by_run(run.run_id)
    branch_audits = [a for a in audits if "branch_id" in a.execution_metadata]
    assert len(branch_audits) == 3
    # All branch audits linked to the same parent run_id
    for audit in branch_audits:
        assert audit.run_id == run.run_id


# ---------------------------------------------------------------------------
# Policy tests
# ---------------------------------------------------------------------------


class RecordingPolicyGuard(PolicyGuard):
    """A PolicyGuard that records all evaluate calls and optionally denies specific nodes."""

    def __init__(self, *, deny_nodes: set[str] | None = None):
        super().__init__()
        self.calls: list[dict[str, Any]] = []
        self._deny_nodes = deny_nodes or set()

    def evaluate(self, graph, node, run, input_payload):
        self.calls.append({
            "node_id": node.node_id,
            "run_id": run.run_id,
            "input_keys": list(dict(input_payload).keys()),
        })
        if node.node_id in self._deny_nodes:
            return EnforcementResult(
                decision=PolicyDecision.DENY,
                reason=f"policy denied {node.node_id}",
            )
        return EnforcementResult(decision=PolicyDecision.ALLOW)


@pytest.mark.asyncio
async def test_per_branch_policy_enforcement(sqlite_db) -> None:
    """Policy guard is called once per branch with branch-isolated context."""
    guard = RecordingPolicyGuard()
    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": _sink_runner()},
        sqlite_db,
        policy_guard=guard,
    )

    run = await orchestrator.run_graph(_fan_out_graph(), {"value": 1})

    assert run.status is RunStatus.COMPLETED
    # Policy should be called for source node (main loop) + 3 branch sink calls
    sink_calls = [c for c in guard.calls if c["node_id"] == "sink"]
    assert len(sink_calls) == 3


@pytest.mark.asyncio
async def test_policy_denial_in_branch(sqlite_db) -> None:
    """Policy denies one branch node, that branch fails, others continue (best-effort)."""
    # Create a guard that denies "sink" (all branches fail)
    # For best-effort, the run should still complete with failed branches
    call_count = 0

    class SelectiveDenyGuard(PolicyGuard):
        """Denies the second branch call to sink."""
        def __init__(self):
            super().__init__()
            self._sink_call_count = 0

        def evaluate(self, graph, node, run, input_payload):
            if node.node_id == "sink":
                self._sink_call_count += 1
                if self._sink_call_count == 2:
                    return EnforcementResult(
                        decision=PolicyDecision.DENY,
                        reason="policy denied branch 2",
                    )
            return EnforcementResult(decision=PolicyDecision.ALLOW)

    guard = SelectiveDenyGuard()
    graph = _make_graph(
        [
            _make_agent_node(
                "source",
                parallel_config=ParallelConfig(split_path="items", fail_mode="best_effort"),
            ),
            _make_agent_node("sink"),
        ],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )
    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": _sink_runner()},
        sqlite_db,
        policy_guard=guard,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    last_output = run.metadata.get("last_output", {})
    items = last_output.get("items", [])
    assert len(items) == 3
    # One branch should have failed (None output)
    none_count = sum(1 for item in items if item is None)
    assert none_count == 1


# ---------------------------------------------------------------------------
# Budget tests
# ---------------------------------------------------------------------------


class MockBudgetEnforcer:
    """Simple mock for BudgetEnforcer that returns configurable results."""

    def __init__(self, *, allowed: bool = True, spend: float = 0.0, cap: float = 100.0):
        self._allowed = allowed
        self._spend = spend
        self._cap = cap
        self.call_count = 0

    async def check_budget(self, tenant_id: str) -> tuple[bool, float, float]:
        self.call_count += 1
        return self._allowed, self._spend, self._cap


@pytest.mark.asyncio
async def test_budget_check_before_spawn(sqlite_db) -> None:
    """BudgetEnforcer.check_budget called before any branch executes; if not allowed, fan-out does not proceed."""
    budget = MockBudgetEnforcer(allowed=False, spend=100.0, cap=50.0)
    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": _sink_runner()},
        sqlite_db,
        budget_enforcer=budget,
    )

    run = await orchestrator.run_graph(_fan_out_graph(), {"value": 1})

    assert run.status is RunStatus.FAILED
    assert budget.call_count >= 1
    assert run.failure_state is not None
    assert "budget exceeded" in run.failure_state.message.lower() or \
           "parallel_execution_failed" in run.failure_state.reason


@pytest.mark.asyncio
async def test_budget_check_allowed_proceeds(sqlite_db) -> None:
    """BudgetEnforcer returns allowed=True, all branches execute normally."""
    budget = MockBudgetEnforcer(allowed=True, spend=10.0, cap=100.0)
    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": _sink_runner()},
        sqlite_db,
        budget_enforcer=budget,
    )

    run = await orchestrator.run_graph(_fan_out_graph(), {"value": 1})

    assert run.status is RunStatus.COMPLETED
    assert budget.call_count >= 1
    last_output = run.metadata.get("last_output", {})
    assert len(last_output.get("items", [])) == 3


@pytest.mark.asyncio
async def test_budget_enforcer_none_skips(sqlite_db) -> None:
    """No budget_enforcer configured, fan-out proceeds without check."""
    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": _sink_runner()},
        sqlite_db,
        budget_enforcer=None,
    )

    run = await orchestrator.run_graph(_fan_out_graph(), {"value": 1})

    assert run.status is RunStatus.COMPLETED
    last_output = run.metadata.get("last_output", {})
    assert len(last_output.get("items", [])) == 3


# ---------------------------------------------------------------------------
# Step limit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_step_limit(sqlite_db) -> None:
    """max_total_steps=5, 3 branches each doing 1 step -- step limit hit if current history is 4."""
    # Source runs as step 1 (history entry). Then 3 branches each do 1 step.
    # Total = 1 (source) + 3 (branches) = 4. With max_total_steps=3,
    # the step tracker should raise (source uses 1, branches try 3 more).
    graph = _make_graph(
        [
            _make_agent_node(
                "source",
                parallel_config=ParallelConfig(split_path="items", fail_mode="fail_fast"),
            ),
            _make_agent_node("sink"),
        ],
        [Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
        max_total_steps=2,  # source uses 1, only 1 branch can proceed
    )

    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": _sink_runner()},
        sqlite_db,
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    # Should fail due to step limit exceeded during parallel execution
    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    assert "parallel_execution_failed" in run.failure_state.reason


# ---------------------------------------------------------------------------
# Branch isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_branch_visit_counts_isolated(sqlite_db) -> None:
    """Each branch starts with empty visit counts per D-05, parent visit counts unchanged."""
    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": _sink_runner()},
        sqlite_db,
    )

    run = await orchestrator.run_graph(_fan_out_graph(), {"value": 1})

    assert run.status is RunStatus.COMPLETED
    # Parent run's visit counts should show source:1, sink:1 (from the post-fan-out increment)
    # but NOT sink:3 (branches don't mutate parent visit counts directly)
    assert run.node_visit_counts.get("source") == 1
    # sink is incremented once during post-fan-out downstream tracking
    assert run.node_visit_counts.get("sink", 0) == 1


# ---------------------------------------------------------------------------
# Contract validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_branch_contract_validation(sqlite_db) -> None:
    """Each branch's output is validated independently -- since contract validation
    happens inside _dispatch_node for agent runners via output_model, this verifies
    that each branch runs _dispatch_node with its own payload."""
    results_seen: list[dict] = []

    def recording_sink_handler(req):
        x = req.metadata["input_payload"].get("x", 0)
        result = {"result": x * 10}
        results_seen.append(result)
        return ProviderResponse(content=result)

    sink_runner = _make_agent_runner(
        input_model=BranchItemInput,
        output_model=ProcessedOutput,
        handler=recording_sink_handler,
    )

    orchestrator = _make_orchestrator(
        {"source": _source_runner(), "sink": sink_runner},
        sqlite_db,
    )

    run = await orchestrator.run_graph(_fan_out_graph(), {"value": 1})

    assert run.status is RunStatus.COMPLETED
    # Each branch should have produced its own validated output
    assert len(results_seen) == 3
    # Results should correspond to x*10 for x in {1, 2, 3}
    result_values = sorted(r["result"] for r in results_seen)
    assert result_values == [10, 20, 30]
