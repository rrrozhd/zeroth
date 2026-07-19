"""The runtime's policy and approval gate collaborator.

``RuntimePolicyGate`` owns every check that can stop a node before it is
dispatched: loop guards, policy evaluation (sequential and per-branch), the
side-effect approval gate, and consumption of a resolved side-effect approval.

It receives its dependencies explicitly. Two of them are callbacks into the
driver — failing a run and refreshing artifact TTLs are the driver's
responsibility, and the gate declares that it needs them rather than reaching
back into an orchestrator to find them.
"""

from __future__ import annotations

from typing import Any

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    ExecutionSettings,
    Graph,
)
from zeroth.core.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.core.runs import Run, RunHistoryEntry, RunStatus
from zeroth.runtime.orchestration import RuntimeAuditRecorder, RuntimePolicyGate


class _CollectingAuditRepository:
    def __init__(self) -> None:
        self.records: list[Any] = []

    async def write(self, record: Any) -> Any:
        self.records.append(record)
        return record


class _EchoRunRepository:
    def __init__(self) -> None:
        self.puts: list[Run] = []
        self.checkpoints: list[Run] = []

    async def put(self, run: Run) -> Run:
        self.puts.append(run)
        return run

    async def write_checkpoint(self, run: Run) -> str:
        self.checkpoints.append(run)
        return "cp"


class _Driver:
    """Stand-in for the two driver callbacks the gate depends on."""

    def __init__(self) -> None:
        self.failures: list[tuple[str, str]] = []
        self.ttl_refreshes: list[str] = []

    async def fail_run(self, run: Run, reason: str, message: str) -> Run:
        self.failures.append((reason, message))
        run.status = RunStatus.FAILED
        return run

    async def refresh_artifact_ttls(self, run: Run) -> None:
        self.ttl_refreshes.append(run.run_id)


def _node(node_id: str = "n1", **kwargs: Any) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref="g:v1",
        agent=AgentNodeData(instruction="i", model_provider="provider://p"),
        **kwargs,
    )


def _run(**kwargs: Any) -> Run:
    defaults: dict[str, Any] = {
        "graph_version_ref": "g:v1",
        "deployment_ref": "d",
        "thread_id": "t",
        "current_node_ids": [],
        "pending_node_ids": [],
        "metadata": {},
    }
    defaults.update(kwargs)
    return Run(**defaults)


def _graph(nodes: list[Any], **kwargs: Any) -> Graph:
    return Graph(
        graph_id="g",
        name="g",
        entry_step=nodes[0].node_id,
        execution_settings=ExecutionSettings(**kwargs) if kwargs else ExecutionSettings(),
        nodes=nodes,
        edges=[],
    )


def _gate(**overrides: Any) -> tuple[RuntimePolicyGate, _Driver]:
    driver = _Driver()
    kwargs: dict[str, Any] = {
        "run_repository": _EchoRunRepository(),
        "audit_recorder": RuntimeAuditRecorder(),
        "fail_run": driver.fail_run,
        "refresh_artifact_ttls": driver.refresh_artifact_ttls,
    }
    kwargs.update(overrides)
    return RuntimePolicyGate(**kwargs), driver


def _denying_guard() -> PolicyGuard:
    capability_registry = CapabilityRegistry()
    capability_registry.register("capability://secret-access", Capability.SECRET_ACCESS)
    policy_registry = PolicyRegistry()
    policy_registry.register(
        PolicyDefinition(
            policy_id="policy://deny",
            denied_capabilities=[Capability.SECRET_ACCESS],
        )
    )
    return PolicyGuard(
        policy_registry=policy_registry,
        capability_registry=capability_registry,
    )


def test_the_gate_takes_its_dependencies_by_injection() -> None:
    """The gate is constructible from explicit dependencies and callbacks."""
    repository = _EchoRunRepository()
    gate, driver = _gate(run_repository=repository)

    assert gate.run_repository is repository
    assert gate.fail_run == driver.fail_run
    assert gate.refresh_artifact_ttls == driver.refresh_artifact_ttls
    # Governance collaborators are optional: an ungoverned runtime allows all.
    assert gate.policy_guard is None
    assert gate.approval_service is None


async def test_loop_guard_fails_the_run_once_the_step_limit_is_reached() -> None:
    gate, driver = _gate()
    run = _run()
    # The guard compares against len(execution_history), so one recorded step
    # already meets a ceiling of one.
    run.execution_history.append(
        RunHistoryEntry(node_id="n1", status="completed", input_snapshot={}, output_snapshot={})
    )
    graph = _graph([_node()], max_total_steps=1)

    failed = await gate.enforce_loop_guards(graph, run, 0.0)

    assert failed is not None
    assert driver.failures == [("max_total_steps", "max total step limit exceeded")]


async def test_loop_guard_passes_when_within_limits() -> None:
    gate, driver = _gate()

    assert await gate.enforce_loop_guards(_graph([_node()]), _run(), 0.0) is None
    assert driver.failures == []


async def test_policy_allow_records_the_granted_capability_set_on_the_run() -> None:
    """An ALLOW stores the decision so later capability checks can read it."""
    gate, driver = _gate(policy_guard=_denying_guard())
    run = _run()
    node = _node()

    assert await gate.enforce_policy(_graph([node]), run, node, {}) is None
    assert driver.failures == []
    assert "n1" in run.metadata["enforcement"]


async def test_policy_denial_writes_a_rejected_audit_then_fails_the_run() -> None:
    repository = _CollectingAuditRepository()
    gate, driver = _gate(
        policy_guard=_denying_guard(),
        audit_recorder=RuntimeAuditRecorder(audit_repository=repository),
    )
    node = _node(
        policy_bindings=["policy://deny"],
        capability_bindings=["capability://secret-access"],
    )
    run = _run()

    denied = await gate.enforce_policy(_graph([node]), run, node, {})

    assert denied is not None
    (record,) = repository.records
    assert record.status == "rejected"
    assert record.execution_metadata["enforcement_applied"] is False
    assert record.error is not None
    assert run.audit_refs == ["audit:1"]
    assert [reason for reason, _ in driver.failures] == ["policy_violation"]


async def test_branch_policy_enforcement_persists_the_decision_per_node() -> None:
    """Branch enforcement mirrors the sequential path's capability persistence."""
    gate, _driver = _gate(policy_guard=_denying_guard())
    run = _run()
    node = _node()

    assert await gate.enforce_policy_for_branch(_graph([node]), run, node, {}) is None
    assert "n1" in run.metadata["enforcement"]


async def test_branch_policy_denial_returns_the_reason_without_failing_the_run() -> None:
    gate, driver = _gate(policy_guard=_denying_guard())
    node = _node(
        policy_bindings=["policy://deny"],
        capability_bindings=["capability://secret-access"],
    )

    reason = await gate.enforce_policy_for_branch(_graph([node]), _run(), node, {})

    assert isinstance(reason, str) and reason
    # A branch denial is raised by the caller, not failed here.
    assert driver.failures == []


def test_effective_capabilities_are_none_only_when_the_guard_is_unwired() -> None:
    """``None`` means enforcement off; an empty set fail-closed denies."""
    ungoverned, _ = _gate()
    assert ungoverned.effective_capabilities_for(_run(), "n1") is None

    governed, _ = _gate(policy_guard=_denying_guard())
    assert governed.effective_capabilities_for(_run(), "n1") == set()


def test_enforcement_context_tolerates_malformed_metadata() -> None:
    gate, _driver = _gate()

    assert gate.enforcement_context_for(_run(metadata={"enforcement": "nope"}), "n1") == {}
    assert gate.enforcement_context_for(_run(metadata={"enforcement": {"n1": 7}}), "n1") == {}
    assert gate.enforcement_context_for(_run(), "n1") == {}


async def test_side_effect_gate_is_a_no_op_when_policy_does_not_require_approval() -> None:
    gate, _driver = _gate()

    assert await gate.gate_policy_required_side_effects(_run(), _node(), {}) is None
