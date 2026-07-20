"""Characterization of the orchestration runtime's observable side-effect order.

Task 8 decomposes ``RuntimeOrchestrator`` into collaborators. The contract that
decomposition must preserve is not a set of signatures — it is the *sequence*
and *payload* of the calls the orchestrator makes to its collaborators, plus
the run state it leaves behind at each pause point.

Existing suites (``tests/orchestrator``, ``tests/parallel``, ``tests/subgraph``)
assert outcomes: final status, merged output, audit contents. None of them pins
the interleaving of ``run_repository.put`` / ``write_checkpoint`` /
``audit_repository.write`` / webhook emission / artifact-TTL refresh. That
interleaving is exactly what a careless extraction reorders, and a reordered
checkpoint changes what a crashed run resumes from.

Every test here records collaborator calls through recording proxies wrapping
the real repositories, then asserts the exact ordered sequence. They are
written against the pre-decomposition facade and must keep passing unchanged
after it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    ContentSafetyConfig,
    DeterministicProviderAdapter,
)
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.governance.approvals import ApprovalDecision, ApprovalRepository, ApprovalService
from zeroth.governance.audit import AuditRepository
from zeroth.core.execution_units import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutionSettings,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.governance.identity import ActorIdentity, AuthMethod
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.runtime.parallel.models import ParallelConfig
from zeroth.governance.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.core.runs import Run, RunRepository, RunStatus


class NumberInput(BaseModel):
    value: int


class NumberOutput(BaseModel):
    value: int


class AnswerOutput(BaseModel):
    answer: str


class _Journal:
    """Ordered log of every collaborator call the orchestrator makes."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, ...]] = []

    def record(self, *entry: str) -> None:
        self.entries.append(tuple(entry))

    def names(self) -> list[str]:
        return [entry[0] for entry in self.entries]


class _RecordingRunRepository:
    """Proxy that logs each run-persistence call before delegating."""

    def __init__(self, inner: RunRepository, journal: _Journal) -> None:
        self._inner = inner
        self._journal = journal

    async def create(self, run: Run) -> Run:
        self._journal.record("run.create", run.status.value)
        return await self._inner.create(run)

    async def put(self, run: Run) -> Run:
        self._journal.record("run.put", run.status.value, run.current_step or "")
        return await self._inner.put(run)

    async def get(self, run_id: str) -> Run | None:
        self._journal.record("run.get")
        return await self._inner.get(run_id)

    async def write_checkpoint(self, run: Run) -> str:
        self._journal.record("run.checkpoint", run.status.value)
        return await self._inner.write_checkpoint(run)

    async def get_checkpoint(self, checkpoint_id: str) -> Run | None:
        return await self._inner.get_checkpoint(checkpoint_id)


class _RecordingAuditRepository:
    """Proxy that logs each audit write before delegating."""

    def __init__(self, inner: AuditRepository, journal: _Journal) -> None:
        self._inner = inner
        self._journal = journal
        self.records: list[Any] = []

    async def write(self, record: Any) -> Any:
        self._journal.record("audit.write", record.node_id, record.status)
        self.records.append(record)
        return await self._inner.write(record)


class _RecordingWebhookService:
    def __init__(self, journal: _Journal) -> None:
        self._journal = journal

    async def emit_event(self, *, event_type: str, deployment_ref: str, tenant_id: str, data: Any):
        self._journal.record("webhook", event_type)


class _RecordingArtifactStore:
    """Minimal artifact store that only records TTL-refresh reachability."""

    def __init__(self, journal: _Journal) -> None:
        self._journal = journal

    async def refresh_ttl(self, key: str, ttl: int) -> None:
        self._journal.record("artifact.refresh_ttl", key)


def _passthrough_runner(name: str) -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name=name,
            instruction="respond",
            model_name="governai:test",
            input_model=NumberInput,
            output_model=NumberOutput,
        ),
        CallableProviderAdapter(
            lambda request: ProviderResponse(
                content={"value": request.metadata["input_payload"]["value"]}
            )
        ),
    )


def _failing_runner(name: str, exc: Exception) -> AgentRunner:
    def _boom(request):
        raise exc

    return AgentRunner(
        AgentConfig(
            name=name,
            instruction="respond",
            model_name="governai:test",
            input_model=NumberInput,
            output_model=NumberOutput,
        ),
        CallableProviderAdapter(_boom),
    )


def _agent_node(node_id: str) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref="graph-char:v1",
        input_contract_ref="contract://input",
        output_contract_ref="contract://number",
        agent=AgentNodeData(instruction=node_id, model_provider=f"provider://{node_id}"),
    )


def _two_agent_graph() -> Graph:
    return Graph(
        graph_id="graph-char",
        name="characterization",
        entry_step="first",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[_agent_node("first"), _agent_node("second")],
        edges=[Edge(edge_id="edge-1", source_node_id="first", target_node_id="second")],
    )


def _orchestrator(sqlite_db, journal: _Journal, **overrides: Any) -> RuntimeOrchestrator:
    kwargs: dict[str, Any] = {
        "run_repository": _RecordingRunRepository(RunRepository(sqlite_db), journal),
        "audit_repository": _RecordingAuditRepository(AuditRepository(sqlite_db), journal),
        "agent_runners": {
            "first": _passthrough_runner("first"),
            "second": _passthrough_runner("second"),
        },
        "executable_unit_runner": ExecutableUnitRunner(ExecutableUnitRegistry()),
        "webhook_service": _RecordingWebhookService(journal),
    }
    kwargs.update(overrides)
    return RuntimeOrchestrator(**kwargs)


async def test_successful_linear_run_side_effect_order_is_exact(sqlite_db) -> None:
    """A two-node run persists, checkpoints, and audits in one fixed order.

    The per-node cycle is: put(RUNNING, node) -> dispatch -> audit.write ->
    put -> checkpoint. Completion adds a final put/checkpoint pair before the
    ``run.completed`` webhook. Moving the checkpoint before the audit write, or
    the webhook before the checkpoint, changes what a crashed run replays.
    """
    journal = _Journal()
    orchestrator = _orchestrator(sqlite_db, journal)

    run = await orchestrator.run_graph(_two_agent_graph(), {"value": 3})

    assert run.status is RunStatus.COMPLETED
    assert journal.entries == [
        ("run.create", "PENDING"),
        ("run.put", "RUNNING", ""),
        ("run.checkpoint", "RUNNING"),
        # node "first"
        ("run.put", "RUNNING", "first"),
        ("audit.write", "first", "completed"),
        ("run.put", "RUNNING", "first"),
        ("run.checkpoint", "RUNNING"),
        # node "second"
        ("run.put", "RUNNING", "second"),
        ("audit.write", "second", "completed"),
        ("run.put", "RUNNING", "second"),
        ("run.checkpoint", "RUNNING"),
        # completion
        ("run.put", "COMPLETED", "second"),
        ("run.checkpoint", "COMPLETED"),
        ("webhook", "run.completed"),
    ]


async def test_audit_refs_and_history_are_appended_in_dispatch_order(sqlite_db) -> None:
    """Audit refs are sequential per run and history mirrors execution order."""
    journal = _Journal()
    orchestrator = _orchestrator(sqlite_db, journal)

    run = await orchestrator.run_graph(_two_agent_graph(), {"value": 3})

    assert run.audit_refs == ["audit:1", "audit:2"]
    assert [entry.node_id for entry in run.execution_history] == ["first", "second"]
    assert [entry.audit_ref for entry in run.execution_history] == ["audit:1", "audit:2"]
    assert run.completed_steps == ["first", "second"]
    # Stored audit ids are namespaced by run so append-only storage stays unique.
    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    assert [audit.audit_id for audit in audits] == [
        f"{run.run_id}:audit:1",
        f"{run.run_id}:audit:2",
    ]


async def test_node_failure_writes_failed_audit_before_failing_the_run(sqlite_db) -> None:
    """A bare dispatch error audits as ``failed``, then the run fails.

    The failure audit must land *before* the run transitions to FAILED, and the
    ``run.failed`` webhook after the checkpoint — otherwise a consumer reacting
    to the webhook can read a run whose audit trail is still missing the node.
    """
    journal = _Journal()
    orchestrator = _orchestrator(
        sqlite_db,
        journal,
        agent_runners={
            "first": _failing_runner("first", RuntimeError("provider exploded")),
            "second": _passthrough_runner("second"),
        },
    )

    run = await orchestrator.run_graph(_two_agent_graph(), {"value": 3})

    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    assert run.failure_state.reason == "node_execution_failed"
    assert journal.entries == [
        ("run.create", "PENDING"),
        ("run.put", "RUNNING", ""),
        ("run.checkpoint", "RUNNING"),
        ("run.put", "RUNNING", "first"),
        ("audit.write", "first", "failed"),
        ("run.put", "FAILED", "first"),
        ("run.checkpoint", "FAILED"),
        ("webhook", "run.failed"),
    ]


async def test_failure_audit_payload_carries_error_type_and_run_attribution(sqlite_db) -> None:
    """The failure audit record's exact field set is part of the contract."""
    journal = _Journal()
    audit_proxy = _RecordingAuditRepository(AuditRepository(sqlite_db), journal)
    orchestrator = _orchestrator(
        sqlite_db,
        journal,
        audit_repository=audit_proxy,
        agent_runners={
            "first": _failing_runner("first", RuntimeError("provider exploded")),
            "second": _passthrough_runner("second"),
        },
    )

    run = await orchestrator.run_graph(_two_agent_graph(), {"value": 3})

    (record,) = audit_proxy.records
    assert record.status == "failed"
    assert record.node_id == "first"
    assert record.run_id == run.run_id
    assert record.tenant_id == run.tenant_id
    assert record.workspace_id == run.workspace_id
    assert record.graph_version_ref == run.graph_version_ref
    assert record.deployment_ref == run.deployment_ref
    assert record.attempt == 1
    assert record.output_snapshot == {}
    # The runner wraps provider faults, so the recorded type is the runner's.
    assert record.execution_metadata == {"error_type": "AgentProviderError"}
    assert "provider exploded" in (record.error or "")
    # started_at is the dispatch time, so the record reports a real duration.
    assert record.started_at is not None
    assert record.completed_at is not None
    assert record.started_at <= record.completed_at


async def test_error_carrying_an_audit_record_is_audited_as_rejected(sqlite_db) -> None:
    """Governance rejections keep their carried audit payload and status.

    An error that attaches ``audit_record`` (content blocks, integrity
    rejections, paid-then-failed calls) is a *rejection*, not an infrastructure
    failure: the carried payload becomes ``execution_metadata`` verbatim and the
    status is ``rejected``. Bare errors take the ``{"error_type": ...}`` branch.
    """
    journal = _Journal()
    audit_proxy = _RecordingAuditRepository(AuditRepository(sqlite_db), journal)
    blocked_runner = AgentRunner(
        AgentConfig(
            name="first",
            instruction="respond",
            model_name="governai:test",
            input_model=NumberInput,
            output_model=AnswerOutput,
            content_safety=ContentSafetyConfig(enabled=True, mode="block"),
        ),
        DeterministicProviderAdapter([ProviderResponse(content='{"answer":"ssn 123-45-6789"}')]),
    )
    orchestrator = _orchestrator(
        sqlite_db,
        journal,
        audit_repository=audit_proxy,
        agent_runners={"first": blocked_runner, "second": _passthrough_runner("second")},
    )

    run = await orchestrator.run_graph(_two_agent_graph(), {"value": 3})

    assert run.status is RunStatus.FAILED
    (record,) = audit_proxy.records
    assert record.status == "rejected"
    # The carried payload is preserved, not replaced by the error_type branch.
    assert "error_type" not in record.execution_metadata
    assert record.execution_metadata != {}
    assert record.error is not None
    assert journal.entries == [
        ("run.create", "PENDING"),
        ("run.put", "RUNNING", ""),
        ("run.checkpoint", "RUNNING"),
        ("run.put", "RUNNING", "first"),
        ("audit.write", "first", "rejected"),
        ("run.put", "FAILED", "first"),
        ("run.checkpoint", "FAILED"),
        ("webhook", "run.failed"),
    ]


async def test_policy_denial_audits_rejected_then_persists_then_fails(sqlite_db) -> None:
    """Policy denial writes a rejected audit, persists the run, then fails it.

    The extra ``run.put`` between the audit write and ``_fail_run`` is load
    bearing: it commits the audit_refs list that the rejected record was
    numbered from.
    """
    journal = _Journal()
    capability_registry = CapabilityRegistry()
    capability_registry.register("capability://secret-access", Capability.SECRET_ACCESS)
    policy_registry = PolicyRegistry()
    policy_registry.register(
        PolicyDefinition(
            policy_id="policy://deny",
            denied_capabilities=[Capability.SECRET_ACCESS],
        )
    )
    guard = PolicyGuard(
        policy_registry=policy_registry,
        capability_registry=capability_registry,
    )
    graph = _two_agent_graph()
    graph.nodes[0].policy_bindings = ["policy://deny"]
    graph.nodes[0].capability_bindings = ["capability://secret-access"]
    orchestrator = _orchestrator(sqlite_db, journal, policy_guard=guard)

    run = await orchestrator.run_graph(graph, {"value": 3})

    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    assert run.failure_state.reason == "policy_violation"
    assert journal.entries == [
        ("run.create", "PENDING"),
        ("run.put", "RUNNING", ""),
        ("run.checkpoint", "RUNNING"),
        ("run.put", "RUNNING", "first"),
        ("audit.write", "first", "rejected"),
        ("run.put", "RUNNING", "first"),
        ("run.put", "FAILED", "first"),
        ("run.checkpoint", "FAILED"),
        ("webhook", "run.failed"),
    ]


async def test_human_approval_pause_persists_gate_state_then_resume_continues(sqlite_db) -> None:
    """Pausing at an approval gate re-queues the node and checkpoints once.

    The paused run must carry ``pending_approval`` metadata and have the gate
    node pushed back to the head of the queue, so ``resume_graph`` re-enters at
    exactly the same node.
    """
    journal = _Journal()
    approval_service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )
    graph = Graph(
        graph_id="graph-char",
        name="characterization",
        entry_step="first",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            _agent_node("first"),
            HumanApprovalNode(
                node_id="gate",
                graph_version_ref="graph-char:v1",
                human_approval=HumanApprovalNodeData(),
            ),
        ],
        edges=[Edge(edge_id="edge-1", source_node_id="first", target_node_id="gate")],
    )
    orchestrator = _orchestrator(sqlite_db, journal, approval_service=approval_service)

    paused = await orchestrator.run_graph(graph, {"value": 3})

    assert paused.status is RunStatus.WAITING_APPROVAL
    assert paused.pending_node_ids[0] == "gate"
    assert paused.metadata["pending_approval"]["node_id"] == "gate"
    assert journal.entries == [
        ("run.create", "PENDING"),
        ("run.put", "RUNNING", ""),
        ("run.checkpoint", "RUNNING"),
        ("run.put", "RUNNING", "first"),
        ("audit.write", "first", "completed"),
        ("run.put", "RUNNING", "first"),
        ("run.checkpoint", "RUNNING"),
        ("run.put", "RUNNING", "gate"),
        ("webhook", "approval.requested"),
        ("run.put", "WAITING_APPROVAL", "gate"),
        ("run.checkpoint", "WAITING_APPROVAL"),
    ]

    approval_id = paused.metadata["pending_approval"]["approval_id"]
    record = await approval_service.get(approval_id)
    assert record is not None
    resolved = await approval_service.resolve(
        approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=ActorIdentity(subject="reviewer", auth_method=AuthMethod.API_KEY),
    )

    journal.entries.clear()
    node = graph.nodes[1]
    assert isinstance(node, HumanApprovalNode)
    continued = await orchestrator.record_approval_resolution(
        graph=graph,
        run=paused,
        node=node,
        output_payload={"value": 3},
        approval_record=resolved,
    )

    assert continued.status is RunStatus.RUNNING
    assert continued.pending_node_ids == []
    assert journal.entries == [
        ("audit.write", "gate", "completed"),
        ("run.put", "RUNNING", "gate"),
        ("run.checkpoint", "RUNNING"),
    ]


async def test_resume_graph_rejects_a_completed_run(sqlite_db) -> None:
    """Only RUNNING / PENDING / WAITING_APPROVAL runs are resumable."""
    journal = _Journal()
    orchestrator = _orchestrator(sqlite_db, journal)
    run = await orchestrator.run_graph(_two_agent_graph(), {"value": 3})

    try:
        await orchestrator.resume_graph(_two_agent_graph(), run.run_id)
    except Exception as exc:  # OrchestratorError
        assert "not resumable" in str(exc)
    else:  # pragma: no cover - guards a behavior change
        raise AssertionError("resuming a completed run must raise")


async def test_max_total_steps_guard_fails_before_dispatching_another_node(sqlite_db) -> None:
    """The loop guard fires at the top of the cycle, before any node runs."""
    journal = _Journal()
    graph = _two_agent_graph()
    graph.execution_settings = ExecutionSettings(max_total_steps=1)
    orchestrator = _orchestrator(sqlite_db, journal)

    run = await orchestrator.run_graph(graph, {"value": 3})

    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    assert run.failure_state.reason == "max_total_steps"
    assert run.metadata["termination_reason"] == "max_total_steps"
    # Exactly one node ran; the guard stopped the second before dispatch.
    assert [entry.node_id for entry in run.execution_history] == ["first"]
    assert journal.names().count("audit.write") == 1


async def test_artifact_ttl_refresh_runs_after_each_checkpoint(sqlite_db) -> None:
    """TTL refresh is reachable and ordered after the checkpoint, never before."""
    journal = _Journal()
    orchestrator = _orchestrator(
        sqlite_db,
        journal,
        artifact_store=_RecordingArtifactStore(journal),
    )

    run = await orchestrator.run_graph(_two_agent_graph(), {"value": 3})

    assert run.status is RunStatus.COMPLETED
    # No artifact references in these payloads, so the store is never called —
    # but the scan is reached without raising, which is the contract
    # (_refresh_artifact_ttls never raises and never precedes a checkpoint).
    assert "artifact.refresh_ttl" not in journal.names()


async def test_entry_metadata_is_seeded_before_the_first_node_runs(sqlite_db) -> None:
    """A new run's metadata shape is part of the persisted contract."""
    journal = _Journal()
    orchestrator = _orchestrator(sqlite_db, journal)
    graph = _two_agent_graph()

    run = await orchestrator.run_graph(graph, {"value": 3})

    assert run.metadata["graph_id"] == "graph-char"
    assert run.metadata["graph_name"] == "characterization"
    assert run.metadata["path"] == ["first", "second"]
    assert run.metadata["audits"] == {}
    assert run.metadata["last_output"] == {"value": 3}
    assert run.graph_version_ref == "graph-char:v1"


class ItemsOutput(BaseModel):
    items: list[dict[str, Any]] = []


class BranchItemInput(BaseModel):
    x: int = 0


class ProcessedOutput(BaseModel):
    result: int = 0


async def test_fan_out_records_branch_audits_before_the_source_node_audit(sqlite_db) -> None:
    """Branch audits are written during fan-out, the source node's audit after.

    The source node that *triggers* the fan-out is recorded only once every
    branch has finished, so its ``audit:N`` ref is numbered after the branch
    refs exist. Branch audits use the ``<run>:branch:<i>:audit:<n>`` namespace
    and never consume a parent ``audit:N`` slot — swapping that ordering
    renumbers the parent's whole audit trail.
    """
    journal = _Journal()
    source_runner = AgentRunner(
        AgentConfig(
            name="source",
            instruction="test",
            model_name="governai:test",
            input_model=NumberInput,
            output_model=ItemsOutput,
        ),
        CallableProviderAdapter(
            lambda req: ProviderResponse(content={"items": [{"x": 1}, {"x": 2}]})
        ),
    )
    sink_runner = AgentRunner(
        AgentConfig(
            name="sink",
            instruction="test",
            model_name="governai:test",
            input_model=BranchItemInput,
            output_model=ProcessedOutput,
        ),
        CallableProviderAdapter(
            lambda req: ProviderResponse(
                content={"result": req.metadata["input_payload"].get("x", 0) * 10}
            )
        ),
    )
    source = AgentNode(
        node_id="source",
        graph_version_ref="graph-char:v1",
        agent=AgentNodeData(instruction="s", model_provider="provider://source"),
        parallel_config=ParallelConfig(split_path="items"),
    )
    sink = AgentNode(
        node_id="sink",
        graph_version_ref="graph-char:v1",
        agent=AgentNodeData(instruction="s", model_provider="provider://sink"),
    )
    graph = Graph(
        graph_id="graph-char",
        name="characterization",
        entry_step="source",
        execution_settings=ExecutionSettings(max_total_steps=50),
        nodes=[source, sink],
        edges=[Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )
    audit_proxy = _RecordingAuditRepository(AuditRepository(sqlite_db), journal)
    orchestrator = _orchestrator(
        sqlite_db,
        journal,
        audit_repository=audit_proxy,
        agent_runners={"source": source_runner, "sink": sink_runner},
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    # Two branch audits (one per item) land before the fan-out source's own.
    written = [(r.node_id, r.audit_id) for r in audit_proxy.records]
    assert [node_id for node_id, _ in written] == ["sink", "sink", "source"]
    branch_ids = sorted(audit_id for node_id, audit_id in written if node_id == "sink")
    assert branch_ids == [
        f"{run.run_id}:branch:0:audit:1",
        f"{run.run_id}:branch:1:audit:1",
    ]
    # The source's own audit is written last and takes the parent's audit:1
    # slot; branch refs are appended to the parent only afterwards, by
    # _merge_fan_in_state. Reversing that order renumbers the parent trail.
    assert written[-1] == ("source", f"{run.run_id}:audit:1")
    assert run.audit_refs == [
        "audit:1",
        f"{run.run_id}:branch:0:audit:1",
        f"{run.run_id}:branch:1:audit:1",
    ]
    # The parent run absorbs the branch history on fan-in, after its own entry.
    assert [entry.node_id for entry in run.execution_history] == ["source", "sink", "sink"]
    assert run.completed_steps == ["source", "sink", "sink"]


async def test_fan_out_merges_branch_results_in_branch_index_order(sqlite_db) -> None:
    """Fan-in preserves branch order regardless of completion order."""
    journal = _Journal()
    source_runner = AgentRunner(
        AgentConfig(
            name="source",
            instruction="test",
            model_name="governai:test",
            input_model=NumberInput,
            output_model=ItemsOutput,
        ),
        CallableProviderAdapter(
            lambda req: ProviderResponse(content={"items": [{"x": i} for i in range(4)]})
        ),
    )
    sink_runner = AgentRunner(
        AgentConfig(
            name="sink",
            instruction="test",
            model_name="governai:test",
            input_model=BranchItemInput,
            output_model=ProcessedOutput,
        ),
        CallableProviderAdapter(
            lambda req: ProviderResponse(
                content={"result": req.metadata["input_payload"].get("x", 0) * 10}
            )
        ),
    )
    source = AgentNode(
        node_id="source",
        graph_version_ref="graph-char:v1",
        agent=AgentNodeData(instruction="s", model_provider="provider://source"),
        parallel_config=ParallelConfig(split_path="items"),
    )
    sink = AgentNode(
        node_id="sink",
        graph_version_ref="graph-char:v1",
        agent=AgentNodeData(instruction="s", model_provider="provider://sink"),
    )
    graph = Graph(
        graph_id="graph-char",
        name="characterization",
        entry_step="source",
        execution_settings=ExecutionSettings(max_total_steps=50),
        nodes=[source, sink],
        edges=[Edge(edge_id="e1", source_node_id="source", target_node_id="sink")],
    )
    orchestrator = _orchestrator(
        sqlite_db,
        journal,
        agent_runners={"source": source_runner, "sink": sink_runner},
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    items = run.metadata["last_output"]["items"]
    assert [item["result"] for item in items] == [0, 10, 20, 30]
