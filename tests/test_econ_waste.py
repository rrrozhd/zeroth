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

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.econ.analytics import (
    EconThresholdError,
    WasteKind,
    analyze_run,
    waste_gate,
)
from zeroth.governance.audit import AuditRepository
from zeroth.governance.audit.models import NodeAuditRecord, TokenUsage
from zeroth.integrations.execution import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.agents import AgentConfig, AgentRunner
from zeroth.runtime.agents.errors import AgentOutputValidationError
from zeroth.runtime.agents.models import RetryPolicy
from zeroth.runtime.agents.provider import CallableProviderAdapter, ProviderResponse
from zeroth.runtime.agents.resilience import CachingProviderAdapter
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.platform.measurement import MeasurementState
from zeroth.runtime.runs import RunStatus


def _audit(
    node_id: str,
    cost_usd: float | None = None,
    *,
    status: str = "completed",
    audit_id: str = "a",
    execution_metadata: dict | None = None,
) -> NodeAuditRecord:
    return NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id=audit_id,
        run_id="r1",
        node_id=node_id,
        graph_version_ref="g:v1",
        deployment_ref="g",
        status=status,
        cost_usd=cost_usd,
        execution_metadata=execution_metadata or {},
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


def test_loop_reexecution_excludes_free_cache_iterations() -> None:
    """A loop whose repeat is a free cache hit flags $0 recoverable, not the average.

    costs=[0.01 paid, 0.00 cache hit]: the repeated iteration was served from
    cache and attributed $0 (see econ.adapter), so the recoverable re-execution
    waste is $0 -- not the ~$0.005 that averaging (node_cost - node_cost/executions)
    would report by smearing the one paid call across both visits.
    """
    audits = [
        _audit(
            "a",
            0.01,
            audit_id="a1",
            execution_metadata={"response": {"metadata": {"cache_hit": False}}},
        ),
        _audit(
            "a",
            0.0,
            audit_id="a2",
            execution_metadata={"response": {"metadata": {"cache_hit": True}}},
        ),
    ]
    report = analyze_run("r1", RunStatus.COMPLETED, audits)
    loop = next(f for f in report.findings if f.kind == WasteKind.LOOP_REEXECUTION)
    assert loop.node_id == "a"
    assert loop.confirmed is False
    assert loop.wasted_usd == 0.0  # the free cache-hit repeat is not recoverable
    assert loop.severity == "info"  # $0 recoverable -> info, never a warning
    assert loop.metadata["executions"] == 2
    # Headline: no flagged dollars. Averaging would have reported ~$0.005.
    assert report.flagged_waste_usd == 0.0


def test_loop_reexecution_counts_only_paid_repeats() -> None:
    """With one free and one paid repeat, only the paid repeat is recoverable.

    costs=[0.01 paid, 0.01 paid, 0.00 cache hit]: keep one paid execution, the
    other paid execution ($0.01) is the recoverable repeat, and the free one adds
    nothing -> $0.01. Averaging would have reported ~$0.0133 (0.02 - 0.02/3).
    Guards against the opposite over-correction "any cache hit -> report $0".
    """
    audits = [
        _audit("a", 0.01, audit_id="a1"),
        _audit("a", 0.01, audit_id="a2"),
        _audit("a", 0.0, audit_id="a3"),
    ]
    report = analyze_run("r1", RunStatus.COMPLETED, audits)
    loop = next(f for f in report.findings if f.kind == WasteKind.LOOP_REEXECUTION)
    assert loop.wasted_usd == pytest.approx(0.01)
    assert loop.severity == "warning"
    assert report.flagged_waste_usd == pytest.approx(0.01)


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
    """Returns a response carrying cost (as an instrumented adapter would) whose.

    content fails output validation — the paid-then-failed case.
    """

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

    Without the runner+runtime cost-on-failure fix the failed audit would carry
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
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
        agent_runners={"n1": _failing_runner()},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {})
    assert run.status is RunStatus.FAILED

    audits = await AuditRepository.for_default_compatibility(sqlite_db).list_by_run(run.run_id)
    failed = [a for a in audits if a.node_id == "n1" and a.status == "failed"]
    assert len(failed) == 1
    assert failed[0].cost_usd == pytest.approx(0.02)  # paid-then-failed spend survived

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
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
        agent_runners={"loop": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {"count": 0})
    assert run.status is RunStatus.COMPLETED

    audits = await AuditRepository.for_default_compatibility(sqlite_db).list_by_run(run.run_id)
    loop_audits = [a for a in audits if a.node_id == "loop"]
    assert len(loop_audits) >= 2  # one audit per visit -> loop multiplicity is real

    report = analyze_run(run.run_id, run.status, audits)
    assert report.flagged_waste_usd > 0
    assert any(f.kind == WasteKind.LOOP_REEXECUTION for f in report.findings)


# --- P2: retry overhead + cache efficiency ---------------------------------------


def test_aggregate_retry_cost_is_not_multiplied_and_blocks_a_complete_decision() -> None:
    """An aggregate two-attempt cost cannot reveal the failed attempt's share."""
    audits = [_audit("a", 0.02, execution_metadata={"extra": {"attempts": 2}})]
    report = analyze_run("r1", RunStatus.COMPLETED, audits)
    retry = next(f for f in report.findings if f.kind == WasteKind.RETRY_OVERHEAD)
    assert retry.confirmed is False
    assert retry.severity == "info"
    assert retry.metadata["attempts"] == 2
    assert retry.wasted_usd == 0.0
    assert report.total_cost_usd == pytest.approx(0.02)
    assert report.flagged_waste_usd == 0.0
    assert report.cost_measurement_complete is False
    with pytest.raises(EconThresholdError, match="incomplete"):
        waste_gate(report, max_flagged_usd=0.01)


def test_estimated_aggregate_retry_is_indeterminate_without_fabricated_dollars() -> None:
    audit = NodeAuditRecord(
        audit_id="a",
        run_id="r1",
        tenant_id="tenant_default",
        node_id="a",
        graph_version_ref="g:v1",
        deployment_ref="g",
        status="completed",
        estimated_cost_usd=0.02,
        cost_measurement=MeasurementState.ESTIMATED,
        execution_metadata={"extra": {"attempts": 2}},
    )

    report = analyze_run("r1", RunStatus.COMPLETED, [audit])

    retry = next(f for f in report.findings if f.kind == WasteKind.RETRY_OVERHEAD)
    assert retry.wasted_usd == 0.0
    assert retry.metadata["aggregate_estimated_cost_usd"] == pytest.approx(0.02)
    assert report.total_cost_usd == 0.0
    assert report.estimated_cost_usd == pytest.approx(0.02)
    assert report.flagged_waste_usd == 0.0
    assert report.cost_measurement_complete is False
    with pytest.raises(EconThresholdError, match="incomplete"):
        waste_gate(report, max_flagged_usd=0.01)


def test_retry_overhead_is_info_on_failed_run() -> None:
    """On a failed run the retry cost is already in the failed-run total -> info only."""
    audits = [_audit("a", 0.01, execution_metadata={"extra": {"attempts": 3}})]
    report = analyze_run("r1", RunStatus.FAILED, audits)
    assert report.confirmed_waste_usd == pytest.approx(0.01)
    assert report.flagged_waste_usd == 0.0  # no double-count
    retry = next(f for f in report.findings if f.kind == WasteKind.RETRY_OVERHEAD)
    assert retry.wasted_usd == 0.0
    assert retry.severity == "info"


def test_cache_finding_only_when_caching_present() -> None:
    """Cache efficiency is reported only when some audit carries a cache_hit flag."""
    no_cache = analyze_run("r1", RunStatus.COMPLETED, [_audit("a", 0.01)])
    assert not any(f.kind == WasteKind.CACHE_INEFFICIENCY for f in no_cache.findings)

    with_cache = analyze_run(
        "r1",
        RunStatus.COMPLETED,
        [
            _audit(
                "a",
                0.01,
                audit_id="1",
                execution_metadata={"response": {"metadata": {"cache_hit": False}}},
            ),
            _audit(
                "b",
                0.0,
                audit_id="2",
                execution_metadata={"response": {"metadata": {"cache_hit": True}}},
            ),
        ],
    )
    cache = next(f for f in with_cache.findings if f.kind == WasteKind.CACHE_INEFFICIENCY)
    assert cache.metadata == {"hits": 1, "misses": 1, "hit_rate": 0.5}
    assert cache.wasted_usd == 0.0  # info, never counted as waste


# --- P2 real-path: signals reach the persisted audit -----------------------------


async def test_cache_hit_reaches_audit_and_is_detected(sqlite_db) -> None:
    """A cache hit's flag survives into the persisted audit, and the detector reads it.

    Pins the nested ``response.metadata.cache_hit`` path against a serialization
    change silently making the detector inert.
    """
    # Constant echo + a bare self-loop: iteration 2's input equals iteration 1's, so
    # it is an exact cache hit. Bounded to 2 steps so a miss AND a hit land in one
    # run's audits (the run stops at the step guard). Cross-run caching wouldn't hit
    # -- each run_graph builds a slightly different prompt.
    cached = CachingProviderAdapter(
        CallableProviderAdapter(
            lambda _req: ProviderResponse(
                content={"value": 1},
                token_usage=TokenUsage(
                    input_tokens=5, output_tokens=2, total_tokens=7, model_name="t"
                ),
            )
        )
    )
    runner = AgentRunner(
        AgentConfig(
            name="loop",
            instruction="echo",
            model_name="t",
            input_model=_ValueOut,
            output_model=_ValueOut,
        ),
        cached,
    )
    graph = Graph(
        graph_id="g-cache",
        name="cache",
        entry_step="loop",
        execution_settings=ExecutionSettings(max_total_steps=2),
        nodes=[
            AgentNode(
                node_id="loop",
                graph_version_ref="g-cache:v1",
                agent=AgentNodeData(instruction="echo", model_provider="p"),
            )
        ],
        edges=[Edge(edge_id="e", source_node_id="loop", target_node_id="loop")],
    )
    # No regulus/cost wiring -> provider is not re-wrapped, so this test isolates the
    # serialization path (the cache flag reaching the persisted audit). The
    # instrumented path -- where a hit must NOT inflate cost or double-bill Regulus --
    # is covered by test_instrumented_cache_hit_costs_zero_through_orchestrator below.
    orch = RuntimeOrchestrator(
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
        agent_runners={"loop": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {"value": 1})  # 2 visits: miss then hit

    audits = await AuditRepository.for_default_compatibility(sqlite_db).list_by_run(run.run_id)
    report = analyze_run(run.run_id, run.status, audits)
    cache = next(f for f in report.findings if f.kind == WasteKind.CACHE_INEFFICIENCY)
    assert cache.metadata == {"hits": 1, "misses": 1, "hit_rate": 0.5}


async def test_instrumented_cache_hit_costs_zero_through_orchestrator(sqlite_db) -> None:
    """End-to-end: with Regulus + cost wiring on, a cache hit costs zero and emits no event.

    The orchestrator wraps the runner's (caching) provider with
    InstrumentedProviderAdapter as the outermost adapter -- the exact wiring the
    no-Regulus sibling test above deliberately avoids. Before the cache-hit
    short-circuit, the hit re-estimated cost on the cached tokens, inflated
    ``total_cost_usd``, and fired a duplicate ExecutionEvent for a call that never
    reached a model. A *priced* model makes the miss's cost non-zero, so the hit's
    zero is a meaningful contrast rather than two zeros.
    """
    from unittest.mock import MagicMock

    from zeroth.econ.analytics.cost import CostEstimator

    cached = CachingProviderAdapter(
        CallableProviderAdapter(
            lambda _req: ProviderResponse(
                content={"value": 1},
                token_usage=TokenUsage(
                    input_tokens=1000,
                    output_tokens=500,
                    total_tokens=1500,
                    model_name="openai/gpt-4o-mini",
                ),
            )
        )
    )
    runner = AgentRunner(
        AgentConfig(
            name="loop",
            instruction="echo",
            model_name="openai/gpt-4o-mini",
            input_model=_ValueOut,
            output_model=_ValueOut,
        ),
        cached,
    )
    graph = Graph(
        graph_id="g-cache-cost",
        name="cache-cost",
        entry_step="loop",
        execution_settings=ExecutionSettings(max_total_steps=2),
        nodes=[
            AgentNode(
                node_id="loop",
                graph_version_ref="g-cache-cost:v1",
                agent=AgentNodeData(instruction="echo", model_provider="p"),
            )
        ],
        edges=[Edge(edge_id="e", source_node_id="loop", target_node_id="loop")],
    )
    regulus = MagicMock()
    orch = RuntimeOrchestrator(
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
        agent_runners={"loop": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        regulus_client=regulus,
        cost_estimator=CostEstimator(),
        deployment_ref="deploy-1",
    )

    run = await orch.run_graph(graph, {"value": 1})  # 2 visits: miss then hit

    audits = await AuditRepository.for_default_compatibility(sqlite_db).list_by_run(run.run_id)
    report = analyze_run(run.run_id, run.status, audits)

    # Two visits: a cold miss with real cost, and a hit that costs nothing.
    assert len(audits) == 2
    assert sorted(a.cost_usd or 0.0 for a in audits) == [0.0, 0.0]
    estimates = sorted(a.estimated_cost_usd or 0.0 for a in audits)
    assert estimates[1] > 0  # the cold miss is priced, but remains labeled estimated
    assert report.total_cost_usd == 0.0
    assert report.estimated_cost_usd == pytest.approx(estimates[1])

    # Exactly one Regulus event -- the miss. The hit emits none (no double-bill).
    regulus.track_execution.assert_called_once()

    # Caching is still detected end-to-end.
    cache = next(f for f in report.findings if f.kind == WasteKind.CACHE_INEFFICIENCY)
    assert cache.metadata == {"hits": 1, "misses": 1, "hit_rate": 0.5}


class _FlakyValidationProvider:
    """Returns invalid output once, then valid -- forces exactly one validation retry."""

    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, request):  # noqa: ANN001
        """First call returns the wrong shape; subsequent calls succeed (costed)."""
        self.calls += 1
        content = {"value": 1} if self.calls >= 2 else {"wrong": "shape"}
        return ProviderResponse(
            content=content,
            cost_usd=0.01,
            token_usage=TokenUsage(input_tokens=5, output_tokens=2, total_tokens=7, model_name="t"),
        )


async def test_retry_overhead_fires_end_to_end(sqlite_db) -> None:
    """A fail-once-then-succeed node records attempts=2, and the detector triggers."""
    runner = AgentRunner(
        AgentConfig(
            name="n",
            instruction="x",
            model_name="t",
            input_model=_AnyIn,
            output_model=_ValueOut,
            retry_policy=RetryPolicy(max_retries=1, use_exponential_backoff=False),
        ),
        _FlakyValidationProvider(),
    )
    graph = Graph(
        graph_id="g-retry",
        name="retry",
        entry_step="n1",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="n1",
                graph_version_ref="g-retry:v1",
                agent=AgentNodeData(instruction="x", model_provider="p"),
            )
        ],
        edges=[],
    )
    orch = RuntimeOrchestrator(
        run_repository=RunRepository.for_default_compatibility(sqlite_db),
        audit_repository=AuditRepository.for_default_compatibility(sqlite_db),
        agent_runners={"n1": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orch.run_graph(graph, {})
    assert run.status is RunStatus.COMPLETED

    audits = await AuditRepository.for_default_compatibility(sqlite_db).list_by_run(run.run_id)
    n1 = next(a for a in audits if a.node_id == "n1")
    assert n1.execution_metadata["attempt"] == 2  # retry recorded in the audit
    report = analyze_run(run.run_id, run.status, audits)
    retry = next(f for f in report.findings if f.kind == WasteKind.RETRY_OVERHEAD)
    assert retry.metadata["attempts"] == 2
    assert retry.wasted_usd == 0.0
    assert report.flagged_waste_usd == 0.0
    assert report.cost_measurement_complete is False
    with pytest.raises(EconThresholdError, match="incomplete"):
        waste_gate(report, max_flagged_usd=0.01)
