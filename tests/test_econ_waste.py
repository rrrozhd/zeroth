"""Tests for economic waste detection (ECON-WASTE-01).

Two layers:

* Unit tests over ``analyze_run`` / ``waste_gate`` with hand-built audit records —
  the confirmed-vs-flagged split and the no-double-count rule.
* A real-path test that drives a paid-then-failed call through the actual runner
  and orchestrator and asserts the spend survives into the persisted audit and is
  picked up as confirmed waste. This proves the cost signal *reaches* the detector
  (a unit test that injected ``cost_usd`` would pass even if production never did).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime import AgentConfig, AgentRunner
from zeroth.core.agent_runtime.errors import AgentOutputValidationError
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.core.audit import AuditRepository
from zeroth.core.audit.models import NodeAuditRecord, TokenUsage
from zeroth.core.econ import (
    EconThresholdError,
    WasteKind,
    analyze_run,
    waste_gate,
)
from zeroth.core.execution_units import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.core.graph import AgentNode, AgentNodeData, Condition, Edge, ExecutionSettings, Graph
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.runs import RunRepository, RunStatus


def _audit(
    node_id: str, cost_usd: float | None = None, *, status: str = "completed", audit_id: str = "a"
) -> NodeAuditRecord:
    return NodeAuditRecord(
        audit_id=audit_id,
        run_id="r1",
        node_id=node_id,
        graph_version_ref="g:v1",
        deployment_ref="g",
        status=status,
        cost_usd=cost_usd,
    )


# --- analyze_run -----------------------------------------------------------------


def test_failed_run_spend_is_confirmed_waste() -> None:
    """A FAILED run's entire spend is confirmed waste (no usable output)."""
    report = analyze_run("r1", RunStatus.FAILED, [_audit("a", 0.01), _audit("b", 0.02)])
    assert report.total_cost_usd == pytest.approx(0.03)
    assert report.confirmed_waste_usd == pytest.approx(0.03)
    assert report.flagged_waste_usd == 0.0
    assert report.waste_ratio == pytest.approx(1.0)
    finding = next(f for f in report.findings if f.kind == WasteKind.PAID_FOR_FAILED_RUN)
    assert finding.confirmed is True
    assert finding.severity == "high"


def test_completed_run_without_repeats_has_no_findings() -> None:
    """A clean completed run flags nothing."""
    report = analyze_run("r1", RunStatus.COMPLETED, [_audit("a", 0.01), _audit("b", 0.02)])
    assert report.findings == []
    assert report.confirmed_waste_usd == 0.0
    assert report.flagged_waste_usd == 0.0


def test_loop_reexecution_is_flagged_not_confirmed() -> None:
    """A node run 3x on a completed run flags ~2/3 of its cost as recoverable."""
    audits = [
        _audit("a", 0.01, audit_id="a1"),
        _audit("a", 0.01, audit_id="a2"),
        _audit("a", 0.01, audit_id="a3"),
    ]
    report = analyze_run("r1", RunStatus.COMPLETED, audits)
    assert report.confirmed_waste_usd == 0.0
    assert report.flagged_waste_usd == pytest.approx(0.02)  # 0.03 total - one productive run
    finding = next(f for f in report.findings if f.kind == WasteKind.LOOP_REEXECUTION)
    assert finding.node_id == "a"
    assert finding.confirmed is False
    assert finding.severity == "warning"
    assert finding.metadata["executions"] == 3


def test_failed_run_with_loop_does_not_double_count() -> None:
    """A looped node inside a failed run stays inside the failed-run total."""
    audits = [_audit("a", 0.01, audit_id="a1"), _audit("a", 0.01, audit_id="a2")]
    report = analyze_run("r1", RunStatus.FAILED, audits)
    assert report.confirmed_waste_usd == pytest.approx(0.02)  # whole spend, counted once
    assert report.flagged_waste_usd == 0.0
    loop = next(f for f in report.findings if f.kind == WasteKind.LOOP_REEXECUTION)
    assert loop.wasted_usd == 0.0
    assert loop.severity == "info"


def test_cost_by_node_and_none_costs() -> None:
    """``None`` costs count as zero and per-node attribution is summed."""
    report = analyze_run(
        "r1", RunStatus.COMPLETED, [_audit("a", 0.01), _audit("b", None), _audit("c", 0.05)]
    )
    assert report.cost_by_node == {"a": pytest.approx(0.01), "b": 0.0, "c": pytest.approx(0.05)}
    assert report.total_cost_usd == pytest.approx(0.06)
    assert report.findings == []


def test_failed_run_with_no_cost_flags_nothing() -> None:
    """A failed run that never spent anything is not waste."""
    report = analyze_run("r1", RunStatus.FAILED, [])
    assert report.findings == []
    assert report.confirmed_waste_usd == 0.0
    assert report.waste_ratio == 0.0


def test_summary_shape() -> None:
    """``summary()`` exposes the headline metrics as a JSON-friendly dict."""
    summary = analyze_run("r1", RunStatus.FAILED, [_audit("a", 0.02)]).summary()
    assert summary["run_id"] == "r1"
    assert summary["confirmed_waste_usd"] == pytest.approx(0.02)
    assert summary["findings"] == 1


# --- waste_gate ------------------------------------------------------------------


def _failed_report():
    return analyze_run("r1", RunStatus.FAILED, [_audit("a", 0.10)])


def test_waste_gate_passes_under_limits() -> None:
    waste_gate(_failed_report(), max_confirmed_usd=1.0, max_waste_ratio=1.0)  # no raise


def test_waste_gate_raises_on_confirmed_usd() -> None:
    with pytest.raises(EconThresholdError, match="confirmed_waste"):
        waste_gate(_failed_report(), max_confirmed_usd=0.05)


def test_waste_gate_raises_on_ratio() -> None:
    with pytest.raises(EconThresholdError, match="waste_ratio"):
        waste_gate(_failed_report(), max_waste_ratio=0.5)  # ratio is 1.0


def test_waste_gate_raises_on_flagged_usd() -> None:
    loop_report = analyze_run(
        "r1",
        RunStatus.COMPLETED,
        [_audit("a", 0.10, audit_id="x"), _audit("a", 0.10, audit_id="y")],
    )
    with pytest.raises(EconThresholdError, match="flagged_waste"):
        waste_gate(loop_report, max_flagged_usd=0.01)


# --- real path: cost on a paid-then-failed call reaches the detector -------------


class _ValueOut(BaseModel):
    value: int


class _AnyIn(BaseModel):
    pass


class _CostedFailingProvider:
    """Returns a response carrying cost (as an instrumented adapter would) whose
    content fails output validation — the paid-then-failed case."""

    async def ainvoke(self, request):  # noqa: ANN001
        """Return a costed response with the wrong shape for ``_ValueOut``."""
        return ProviderResponse(
            content={"unexpected": "shape"},
            cost_usd=0.02,
            token_usage=TokenUsage(
                input_tokens=10, output_tokens=5, total_tokens=15, model_name="t"
            ),
        )


def _failing_runner() -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name="n",
            instruction="x",
            model_name="t",
            input_model=_AnyIn,
            output_model=_ValueOut,
        ),
        _CostedFailingProvider(),
    )


async def test_runner_attaches_cost_to_validation_failure() -> None:
    """The runner bundles the paid response's cost onto the validation error."""
    with pytest.raises(AgentOutputValidationError) as excinfo:
        await _failing_runner().run({})
    audit = excinfo.value.audit_record
    assert audit["cost_usd"] == pytest.approx(0.02)
    assert audit["token_usage"]["total_tokens"] == 15


async def test_failed_run_cost_survives_into_audit_and_waste_report(sqlite_db) -> None:
    """End-to-end: a paid-then-failed node records its cost, and analyze_run flags it.

    Without the runner+runtime cost-on-failure fix the rejected audit would carry
    no cost and ``paid_for_failed_run`` would silently under-report — this is the
    proof the signal reaches the detector through the real execution path.
    """
    graph = Graph(
        graph_id="g-fail",
        name="fail",
        entry_step="n1",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="n1",
                graph_version_ref="g-fail:v1",
                agent=AgentNodeData(instruction="x", model_provider="p"),
            )
        ],
        edges=[],
    )
    orch = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
        agent_runners={"n1": _failing_runner()},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {})
    assert run.status is RunStatus.FAILED

    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    rejected = [a for a in audits if a.node_id == "n1" and a.status == "rejected"]
    assert len(rejected) == 1
    assert rejected[0].cost_usd == pytest.approx(0.02)  # paid-then-failed spend survived

    report = analyze_run(run.run_id, run.status, audits)
    assert report.confirmed_waste_usd == pytest.approx(0.02)
    assert any(f.kind == WasteKind.PAID_FOR_FAILED_RUN for f in report.findings)


class _CountIn(BaseModel):
    count: int = 0


class _CountOut(BaseModel):
    count: int


async def test_real_loop_yields_multiple_audits_and_flags_waste(sqlite_db) -> None:
    """A real terminating cycle produces one audit per visit, which analyze_run flags.

    Guards loop_reexecution against being inert: confirms the audit data model
    *appends* a record per node visit (rather than superseding to one record), so
    the repeated spend is visible to the detector on a completed run.
    """

    def _increment(request):  # noqa: ANN001
        count = request.metadata["input_payload"].get("count", 0)
        return ProviderResponse(content={"count": count + 1}, cost_usd=0.01)

    runner = AgentRunner(
        AgentConfig(
            name="loop",
            instruction="increment",
            model_name="t",
            input_model=_CountIn,
            output_model=_CountOut,
        ),
        CallableProviderAdapter(_increment),
    )
    graph = Graph(
        graph_id="g-loop",
        name="loop",
        entry_step="loop",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="loop",
                graph_version_ref="g-loop:v1",
                agent=AgentNodeData(instruction="increment", model_provider="p"),
            )
        ],
        # Self-loop while count < 2, then no edge matches -> run completes.
        edges=[
            Edge(
                edge_id="e-loop",
                source_node_id="loop",
                target_node_id="loop",
                condition=Condition(expression="payload.count < 2", allow_cycle_traversal=True),
            )
        ],
    )
    orch = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
        agent_runners={"loop": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {"count": 0})
    assert run.status is RunStatus.COMPLETED

    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    loop_audits = [a for a in audits if a.node_id == "loop"]
    assert len(loop_audits) >= 2  # one audit per visit -> loop multiplicity is real

    report = analyze_run(run.run_id, run.status, audits)
    assert report.flagged_waste_usd > 0
    assert any(f.kind == WasteKind.LOOP_REEXECUTION for f in report.findings)
