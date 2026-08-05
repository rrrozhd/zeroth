"""Durable approval lifecycle regressions for governed LangGraph tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import operator
import pickle
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, Send

from tests.integrations.langgraph.test_enforcement_attestations import _signer, _token
from zeroth.core.langgraph_gateway.context import ReservedContextCodec
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.integrations.langgraph._approval_lifecycle import (
    ApprovalCoordinator,
    ApprovalDecision,
    ApprovalRecord,
    ApprovalResolution,
    ApprovalState,
    SQLiteApprovalRepository,
)
from zeroth.integrations.langgraph._tool_errors import (
    ApprovalRequiresThreadError,
    PolicyViolation,
    ToolGovernanceError,
)
from zeroth.integrations.langgraph._tool_guard import aguard_tool_call, guard_tool_call
from zeroth.integrations.langgraph._tool_normalize import normalize_tool_action
from zeroth.integrations.langgraph._tool_types import (
    SideEffectClass,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)
from zeroth.integrations.langgraph._wrapper import govern_graph
from zeroth.integrations.langgraph import (
    InventoryCoverage,
    LangGraphGatewayClient,
    ToolIdentity,
    ToolInventory,
    ToolInventoryEntry,
    govern_tools,
)

CONTEXT = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-1",
    run_id="run-1",
    thread_id="thread-1",
)
ACTION = normalize_tool_action(
    name="delete_record",
    arguments={"table": "invoices", "id": 41},
    context=CONTEXT,
    side_effect=SideEffectClass.SIDE_EFFECTING,
    tool_call_id="call-1",
)
HOLD = ToolDecision(
    kind=ToolDecisionKind.REQUIRE_APPROVAL,
    reason_code="policy_violation",
    approval_ref="approval-7",
)
ALLOW = ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="unknown_error")
DENY = ToolDecision(kind=ToolDecisionKind.DENY, reason_code="policy_violation")


class SuspendedError(Exception):
    """Test pause signal."""


@dataclass(frozen=True)
class Interrupt:
    value: Any
    id: str


@dataclass(frozen=True)
class StateSnapshot:
    values: dict[str, Any]
    next: tuple[str, ...]
    config: dict[str, Any]
    metadata: dict[str, Any]
    created_at: str | None
    parent_config: dict[str, Any] | None
    tasks: tuple[Any, ...]
    interrupts: tuple[Interrupt, ...]


class PersistentSaver(BaseCheckpointSaver[str]):
    """Small file-backed saver used to prove cross-instance checkpoint persistence."""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self._lock = threading.RLock()
        self._memory = InMemorySaver(serde=self.serde)
        if path.exists():
            stored = pickle.loads(path.read_bytes())
            self._memory.storage = defaultdict(lambda: defaultdict(dict))
            for thread_id, namespaces in stored["storage"].items():
                for namespace, checkpoints in namespaces.items():
                    self._memory.storage[thread_id][namespace].update(checkpoints)
            self._memory.writes = defaultdict(dict, stored["writes"])
            self._memory.blobs = stored["blobs"]

    def _save(self) -> None:
        storage = {
            thread_id: {
                namespace: dict(checkpoints) for namespace, checkpoints in namespaces.items()
            }
            for thread_id, namespaces in self._memory.storage.items()
        }
        self.path.write_bytes(
            pickle.dumps(
                {
                    "storage": storage,
                    "writes": dict(self._memory.writes),
                    "blobs": self._memory.blobs,
                }
            )
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with self._lock:
            return self._memory.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        with self._lock:
            return iter(tuple(self._memory.list(config, filter=filter, before=before, limit=limit)))

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        with self._lock:
            saved = self._memory.put(config, checkpoint, metadata, new_versions)
            self._save()
            return saved

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        with self._lock:
            self._memory.put_writes(config, writes, task_id, task_path)
            self._save()


class AsyncOnlyPersistentSaver(PersistentSaver):
    """File-backed saver that fails if coordinator code uses a sync entrypoint."""

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        del config
        raise AssertionError("sync get_tuple was used")

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        del config, filter, before, limit
        raise AssertionError("sync list was used")

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        del config, checkpoint, metadata, new_versions
        raise AssertionError("sync put was used")

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        del config, writes, task_id, task_path
        raise AssertionError("sync put_writes was used")

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with self._lock:
            return self._memory.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        with self._lock:
            rows = tuple(self._memory.list(config, filter=filter, before=before, limit=limit))
        for row in rows:
            yield row

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        with self._lock:
            saved = self._memory.put(config, checkpoint, metadata, new_versions)
            self._save()
            return saved

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        with self._lock:
            self._memory.put_writes(config, writes, task_id, task_path)
            self._save()


@dataclass
class Pause:
    payload: dict[str, Any] | None = None

    def __call__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        raise SuspendedError


@dataclass
class SequencedClient:
    fresh: ToolDecision = ALLOW
    actions: list[dict[str, Any]] = field(default_factory=list)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        self.actions.append(dict(action.arguments))
        return self.fresh if len(self.actions) >= 2 else HOLD


@dataclass
class PerToolApprovalClient:
    """Require one approval, then allow only the post-consume revalidation."""

    calls: dict[str, int] = field(default_factory=dict)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        del context
        name = action.identity.name
        self.calls[name] = self.calls.get(name, 0) + 1
        if self.calls[name] >= 2:
            return ALLOW
        return ToolDecision(
            ToolDecisionKind.REQUIRE_APPROVAL,
            "policy_violation",
            approval_ref=f"approval-{name}",
        )


@dataclass
class IdenticalCallApprovalClient:
    """Issue distinct approvals while the initial parallel calls are held."""

    hold: bool = True
    issued: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        del action, context
        with self.lock:
            if not self.hold:
                return ALLOW
            self.issued += 1
            return ToolDecision(
                ToolDecisionKind.REQUIRE_APPROVAL,
                "policy_violation",
                approval_ref=f"approval-identical-{self.issued}",
            )


@dataclass
class RecordingSubmitter:
    records: list[NodeAuditRecord] = field(default_factory=list)

    def submit(self, record: NodeAuditRecord) -> None:
        self.records.append(record)


class ParallelState(TypedDict):
    done: Annotated[list[str], operator.add]


def _parallel_builder(
    repository: SQLiteApprovalRepository,
    policy: PerToolApprovalClient,
    executed: list[str],
) -> StateGraph[ParallelState, None, ParallelState, ParallelState]:
    def node(name: str) -> Any:
        action = normalize_tool_action(
            name=name,
            arguments={"record": name},
            context=CONTEXT,
            side_effect=SideEffectClass.SIDE_EFFECTING,
            tool_call_id=f"call-{name}",
        )

        def run(_state: ParallelState) -> ParallelState:
            guard_tool_call(
                action,
                CONTEXT,
                lambda: executed.append(name),
                client=policy,
                approval_lifecycle=repository,
            )
            return {"done": [name]}

        return run

    builder = StateGraph(ParallelState)
    builder.add_node("tool_a", node("tool_a"))
    builder.add_node("tool_b", node("tool_b"))
    builder.add_edge(START, "tool_a")
    builder.add_edge(START, "tool_b")
    builder.add_edge("tool_a", END)
    builder.add_edge("tool_b", END)
    return builder


@dataclass
class ResumeConcurrencyProbe:
    """Expose concurrent graph invokes while leaving the real graph underneath."""

    delegate: Any
    checkpointer: BaseCheckpointSaver[Any]
    coordinate: bool = False
    active: int = 0
    max_active: int = 0
    barrier: threading.Barrier = field(default_factory=lambda: threading.Barrier(2))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get_state(self, config: RunnableConfig) -> Any:
        snapshot = self.delegate.get_state(config)
        if self.coordinate:
            with contextlib.suppress(threading.BrokenBarrierError):
                self.barrier.wait(timeout=0.25)
        return snapshot

    def invoke(self, command: Any, config: RunnableConfig) -> Any:
        if not self.coordinate:
            return self.delegate.invoke(command, config)
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            return self.delegate.invoke(command, config)
        finally:
            with self.lock:
                self.active -= 1

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


def _snapshot(
    payload: dict[str, Any] | None, *, checkpoint_id: str = "checkpoint-1"
) -> StateSnapshot:
    interrupts = () if payload is None else (Interrupt(value=payload, id="interrupt-1"),)
    return StateSnapshot(
        values={},
        next=() if payload is None else ("tools",),
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": checkpoint_id}},
        metadata={},
        created_at=None,
        parent_config=None,
        tasks=(),
        interrupts=interrupts,
    )


@dataclass
class Graph:
    state: StateSnapshot
    checkpointer: BaseCheckpointSaver[Any]
    callback: Any = None
    calls: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)

    def get_state(self, config: dict[str, Any]) -> StateSnapshot:
        assert config["configurable"]["thread_id"] == "thread-1"
        return self.state

    async def aget_state(self, config: dict[str, Any]) -> StateSnapshot:
        return self.get_state(config)

    def invoke(self, command: Any, config: dict[str, Any]) -> Any:
        self.calls.append((command, config))
        resume = command.resume
        if isinstance(resume, dict) and "interrupt-1" in resume:
            resume = resume["interrupt-1"]
        result = None if self.callback is None else self.callback(resume)
        self.state = _snapshot(None, checkpoint_id="checkpoint-2")
        return result

    async def ainvoke(self, command: Any, config: dict[str, Any]) -> Any:
        return self.invoke(command, config)


def _request(
    repository: SQLiteApprovalRepository,
    client: Any,
    pause: Pause,
    *,
    audit: RecordingSubmitter | None = None,
) -> None:
    with pytest.raises(SuspendedError):
        guard_tool_call(
            ACTION,
            CONTEXT,
            lambda: pytest.fail("tool ran before approval"),
            client=client,
            interrupt=pause,
            approval_lifecycle=repository,
            audit=audit,
        )


def _ready(
    tmp_path: Any,
    client: Any | None = None,
    *,
    audit: RecordingSubmitter | None = None,
) -> tuple[SQLiteApprovalRepository, ApprovalCoordinator, Any, SequencedClient]:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    policy = client or SequencedClient()
    pause = Pause()
    _request(repository, policy, pause, audit=audit)
    assert pause.payload is not None
    saver = PersistentSaver(tmp_path / "checkpoints.bin")

    def resume(value: Any) -> None:
        guard_tool_call(
            ACTION,
            CONTEXT,
            lambda: None,
            client=policy,
            interrupt=lambda _payload: value,
            approval_lifecycle=repository,
            audit=audit,
        )

    graph = govern_graph(Graph(_snapshot(pause.payload), saver, callback=resume))
    coordinator = ApprovalCoordinator(repository)
    coordinator.confirm_checkpoint("approval-7", graph, config={}, durable_checkpointer=saver)
    return repository, coordinator, graph, policy


def _resume(
    coordinator: ApprovalCoordinator,
    graph: Any,
    *,
    owner: str,
    config: Mapping[str, Any] | None = None,
) -> ApprovalRecord:
    return coordinator.resume(
        "approval-7",
        graph,
        owner=owner,
        config={} if config is None else config,
        durable_checkpointer=graph.checkpointer,
    )


def test_payload_round_trip_is_persisted_before_interrupt(tmp_path: Any) -> None:
    path = tmp_path / "approvals.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE approvals (marker TEXT)")
        connection.execute("INSERT INTO approvals VALUES ('core-table')")
    repository = SQLiteApprovalRepository(path)
    pause = Pause()

    _request(repository, SequencedClient(), pause)

    record = repository.get("approval-7")
    assert record.state is ApprovalState.AWAITING_CHECKPOINT
    assert dict(record.intent.payload) == pause.payload
    assert dict(record.intent.arguments) == {"table": "invoices", "id": 41}
    reopened = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3").get("approval-7")
    assert reopened.intent == record.intent
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT marker FROM approvals").fetchone() == ("core-table",)
    invalid = ApprovalResolution("approval-7", ApprovalDecision.APPROVE).to_payload()
    invalid["version"] = True
    with pytest.raises(ToolGovernanceError):
        ApprovalResolution.from_payload(invalid)


def test_existing_lifecycle_schema_is_migrated_without_replacement(tmp_path: Any) -> None:
    path = tmp_path / "approvals.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """CREATE TABLE langgraph_approval_lifecycle (
            approval_ref TEXT PRIMARY KEY, state TEXT NOT NULL, intent TEXT NOT NULL,
            resolution TEXT, checkpoint_id TEXT, owner TEXT, lease_deadline REAL,
            deadline REAL NOT NULL);
            CREATE TABLE langgraph_approval_events (
            id INTEGER PRIMARY KEY, approval_ref TEXT NOT NULL, from_state TEXT,
            to_state TEXT NOT NULL, accepted INTEGER NOT NULL, occurred_at REAL NOT NULL);"""
        )
        for ref in ("approval-old-1", "approval-old-2"):
            intent = json.dumps(
                {
                    "version": 1,
                    "payload": {
                        "approval_ref": ref,
                        "tenant_id": "tenant-a",
                        "principal_id": "principal-1",
                        "run_id": "run-1",
                        "thread_id": "thread-1",
                        "tool_fingerprint": "tool-1",
                        "tool_call_id": None,
                        "argument_fingerprint": "arguments-1",
                    },
                    "arguments": {},
                }
            )
            connection.execute(
                "INSERT INTO langgraph_approval_lifecycle "
                "(approval_ref, state, intent, deadline) VALUES (?, ?, ?, ?)",
                (ref, ApprovalState.AWAITING_CHECKPOINT.value, intent, 100.0),
            )
    repository = SQLiteApprovalRepository(path)

    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(langgraph_approval_lifecycle)")
        }
        indexes = connection.execute("PRAGMA index_list(langgraph_approval_lifecycle)").fetchall()
    assert {"interrupt_id", "claim_token", "claim_consumed"} <= columns
    assert any(row[1] == "langgraph_approval_active_identity" and row[2] for row in indexes)
    assert repository.get("approval-old-1").state is ApprovalState.AWAITING_CHECKPOINT
    assert repository.get("approval-old-2").state is ApprovalState.ORPHANED
    assert len(repository.pending()) == 1


def test_lifecycle_rejects_invalid_transitions_and_expires_bounded_work(tmp_path: Any) -> None:
    now = [10.0]
    repository = SQLiteApprovalRepository(
        tmp_path / "approvals.sqlite3", ttl_seconds=5, clock=lambda: now[0]
    )
    pause = Pause()
    _request(repository, SequencedClient(), pause)

    with pytest.raises(ToolGovernanceError):
        repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))
    refused = repository.events("approval-7")[-1]
    assert not refused.accepted and refused.to_state is ApprovalState.DECIDED

    now[0] = 16.0
    [expired] = repository.expire_due(limit=1)
    assert expired.state is ApprovalState.EXPIRED
    assert repository.pending(limit=1) == ()


@pytest.mark.parametrize("setting", ["ttl_seconds", "lease_seconds"])
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=("nan", "positive-infinity", "negative-infinity"),
)
def test_restart_configuration_rejects_non_finite_deadlines(
    tmp_path: Any, setting: str, value: float
) -> None:
    with pytest.raises(ToolGovernanceError, match="finite and positive"):
        SQLiteApprovalRepository(
            tmp_path / "approvals.sqlite3",
            **{setting: value},
        )


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=("nan", "positive-infinity", "negative-infinity"),
)
def test_non_finite_clock_cannot_persist_an_approval_across_restart(
    tmp_path: Any, value: float
) -> None:
    path = tmp_path / "approvals.sqlite3"
    repository = SQLiteApprovalRepository(path, clock=lambda: value)

    with pytest.raises(ToolGovernanceError, match="clock.*finite"):
        guard_tool_call(
            ACTION,
            CONTEXT,
            lambda: pytest.fail("tool ran before approval"),
            client=SequencedClient(),
            interrupt=Pause(),
            approval_lifecycle=repository,
        )

    reopened = SQLiteApprovalRepository(path)
    assert reopened.pending() == ()
    with pytest.raises(ToolGovernanceError, match="does not exist"):
        reopened.get("approval-7")


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=("nan", "positive-infinity", "negative-infinity"),
)
def test_restart_reconciliation_rejects_non_finite_clock_without_mutation(
    tmp_path: Any, value: float
) -> None:
    path = tmp_path / "approvals.sqlite3"
    repository = SQLiteApprovalRepository(path, ttl_seconds=5, clock=lambda: 10.0)
    _request(repository, SequencedClient(), Pause())

    reopened = SQLiteApprovalRepository(path, clock=lambda: value)
    with pytest.raises(ToolGovernanceError, match="clock.*finite"):
        reopened.expire_due()
    assert reopened.get("approval-7").state is ApprovalState.AWAITING_CHECKPOINT

    due = SQLiteApprovalRepository(path, clock=lambda: 16.0)
    [expired] = due.expire_due()
    assert expired.state is ApprovalState.EXPIRED


def test_expire_due_selects_an_expired_consumed_lease_before_non_due_backlog(
    tmp_path: Any,
) -> None:
    now = [0.0]
    path = tmp_path / "approvals.sqlite3"
    consumed_repository = SQLiteApprovalRepository(
        path,
        ttl_seconds=100,
        lease_seconds=1,
        clock=lambda: now[0],
    )
    pause = Pause()
    _request(consumed_repository, SequencedClient(), pause)
    consumed_repository.ready("approval-7", "checkpoint-1", "interrupt-1")
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)
    consumed_repository.decide(resolution)
    claimed = consumed_repository.claim("approval-7", owner="crashed-worker")
    assert claimed.claim_token is not None
    consumed_repository.consume({**resolution.to_payload(), "claim_token": claimed.claim_token})

    backlog_repository = SQLiteApprovalRepository(
        path,
        ttl_seconds=10,
        lease_seconds=1,
        clock=lambda: now[0],
    )
    payload = dict(consumed_repository.get("approval-7").intent.payload)
    for index in range(3):
        backlog_repository.begin(
            {
                **payload,
                "approval_ref": f"approval-backlog-{index}",
                "tool_call_id": f"call-backlog-{index}",
                "argument_fingerprint": f"arguments-backlog-{index}",
            },
            {},
        )

    now[0] = 2.0
    [expired] = backlog_repository.expire_due(limit=2)

    assert expired.intent.payload["approval_ref"] == "approval-7"
    assert expired.state is ApprovalState.ORPHANED
    assert all(
        backlog_repository.get(f"approval-backlog-{index}").state
        is ApprovalState.AWAITING_CHECKPOINT
        for index in range(3)
    )


def test_checkpoint_must_match_before_an_approval_becomes_ready(tmp_path: Any) -> None:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    pause = Pause()
    _request(repository, SequencedClient(), pause)
    assert pause.payload is not None
    coordinator = ApprovalCoordinator(repository)
    saver = PersistentSaver(tmp_path / "checkpoints.bin")
    graph = govern_graph(Graph(_snapshot({"other": "interrupt"}), saver))

    with pytest.raises(ToolGovernanceError):
        coordinator.confirm_checkpoint("approval-7", graph, config={}, durable_checkpointer=saver)

    assert repository.get("approval-7").state is ApprovalState.ORPHANED


def test_idempotent_begin_decision_and_claim_do_not_duplicate_work(tmp_path: Any) -> None:
    repository, coordinator, graph, _ = _ready(tmp_path)
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(repository.decide, (resolution, resolution)))
    assert decisions[0] == decisions[1]
    with pytest.raises(ToolGovernanceError):
        repository.decide(ApprovalResolution("approval-7", ApprovalDecision.REJECT))
    with ThreadPoolExecutor(max_workers=2) as pool:
        resumes = list(
            pool.map(
                lambda _attempt: _resume(coordinator, graph, owner="worker-1"),
                (1, 2),
            )
        )
    assert all(
        record.state in (ApprovalState.RESUMING, ApprovalState.RESOLVED) for record in resumes
    )
    assert repository.get("approval-7").state is ApprovalState.RESOLVED
    assert len(graph.calls) == 1


def test_concurrent_first_delivery_returns_one_canonical_active_approval(tmp_path: Any) -> None:
    path = tmp_path / "approvals.sqlite3"
    repositories = (SQLiteApprovalRepository(path), SQLiteApprovalRepository(path))
    barrier = threading.Barrier(2)
    action = normalize_tool_action(
        name="delete_record",
        arguments={"table": "invoices", "id": 41},
        context=CONTEXT,
        side_effect=SideEffectClass.SIDE_EFFECTING,
    )

    @dataclass
    class RacingClient:
        approval_ref: str

        def decide(self, requested: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
            del requested, context
            barrier.wait(timeout=2)
            return ToolDecision(
                ToolDecisionKind.REQUIRE_APPROVAL,
                "policy_violation",
                approval_ref=self.approval_ref,
            )

    def deliver(attempt: tuple[SQLiteApprovalRepository, str]) -> dict[str, Any]:
        repository, approval_ref = attempt
        pause = Pause()
        with pytest.raises(SuspendedError):
            guard_tool_call(
                action,
                CONTEXT,
                lambda: pytest.fail("tool ran before approval"),
                client=RacingClient(approval_ref),
                interrupt=pause,
                approval_lifecycle=repository,
            )
        assert pause.payload is not None
        return pause.payload

    with ThreadPoolExecutor(max_workers=2) as pool:
        payloads = list(
            pool.map(
                deliver,
                zip(repositories, ("approval-race-1", "approval-race-2"), strict=True),
            )
        )

    assert payloads[0] == payloads[1]
    [active] = repositories[0].pending()
    identity_fields = (
        "tenant_id",
        "principal_id",
        "run_id",
        "thread_id",
        "tool_fingerprint",
        "tool_call_id",
        "argument_fingerprint",
    )
    identity = {key: active.intent.payload[key] for key in identity_fields}
    assert repositories[0].replay_for(identity) == active
    assert repositories[1].replay_for(identity) == active

    canonical_ref = str(active.intent.payload["approval_ref"])
    repositories[0].terminal(canonical_ref, ApprovalState.ORPHANED)
    later_payload = {**payloads[0], "approval_ref": "approval-later"}
    later, created = repositories[1].begin_once(later_payload, action.arguments)
    assert created and later.intent.payload["approval_ref"] == "approval-later"


@pytest.mark.parametrize(
    ("resolution", "fresh", "expected"),
    [
        (ApprovalResolution("approval-7", ApprovalDecision.REJECT), ALLOW, []),
        (ApprovalResolution("approval-7", ApprovalDecision.APPROVE), DENY, []),
        (
            ApprovalResolution(
                "approval-7", ApprovalDecision.APPROVE, {"table": "invoices", "id": 42}
            ),
            ALLOW,
            [{"table": "invoices", "id": 42}],
        ),
    ],
)
def test_revalidation_uses_edited_arguments_and_fresh_deny_wins(
    tmp_path: Any,
    resolution: ApprovalResolution,
    fresh: ToolDecision,
    expected: list[dict[str, Any]],
) -> None:
    policy = SequencedClient(fresh=fresh)
    repository, coordinator, graph, _ = _ready(tmp_path, policy)
    executed: list[dict[str, Any]] = []

    def resume(value: Any) -> None:
        guard_tool_call(
            ACTION,
            CONTEXT,
            lambda: executed.append(dict(ACTION.arguments)),
            client=policy,
            interrupt=lambda _payload: value,
            approval_lifecycle=repository,
            invoke_with_arguments=lambda arguments: executed.append(dict(arguments)),
        )

    graph._graph.callback = resume
    repository.decide(resolution)
    with contextlib.suppress(PolicyViolation):
        _resume(coordinator, graph, owner="worker-1")

    assert executed == expected
    assert repository.get("approval-7").state is ApprovalState.RESOLVED
    if resolution.arguments is not None:
        assert policy.actions[-1] == dict(resolution.arguments)


def test_restart_after_resume_claim_inspects_checkpoint_before_retry(tmp_path: Any) -> None:
    repository, coordinator, graph, _ = _ready(tmp_path)
    repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))

    def crash_after_resume(_value: Any) -> None:
        graph._graph.state = _snapshot(None, checkpoint_id="checkpoint-2")
        raise KeyboardInterrupt

    graph._graph.callback = crash_after_resume
    with pytest.raises(KeyboardInterrupt):
        _resume(coordinator, graph, owner="worker-1")

    reopened = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    _resume(ApprovalCoordinator(reopened), graph, owner="worker-2")
    assert reopened.get("approval-7").state is ApprovalState.ORPHANED
    assert len(graph.calls) == 1


def test_consumed_claim_is_orphaned_after_lease_expiry_on_restart(tmp_path: Any) -> None:
    now = [10.0]
    path = tmp_path / "approvals.sqlite3"
    repository = SQLiteApprovalRepository(
        path, ttl_seconds=100, lease_seconds=1, clock=lambda: now[0]
    )
    pause = Pause()
    _request(repository, SequencedClient(), pause)
    assert pause.payload is not None
    repository.ready("approval-7", "checkpoint-1", "interrupt-1")
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)
    repository.decide(resolution)
    claimed = repository.claim("approval-7", owner="crashed-worker")
    assert claimed.claim_token is not None
    repository.consume({**resolution.to_payload(), "claim_token": claimed.claim_token})

    now[0] = 12.0
    reopened = SQLiteApprovalRepository(
        path, ttl_seconds=100, lease_seconds=1, clock=lambda: now[0]
    )
    saver = PersistentSaver(tmp_path / "checkpoints.bin")
    graph = govern_graph(Graph(_snapshot(pause.payload), saver))

    record = ApprovalCoordinator(reopened).resume(
        "approval-7",
        graph,
        owner="restart-worker",
        config={},
        durable_checkpointer=saver,
    )

    assert record.state is ApprovalState.ORPHANED
    assert reopened.pending() == ()
    assert graph._graph.calls == []


def test_original_thread_receives_a_current_snapshot_langgraph_command(tmp_path: Any) -> None:
    repository, coordinator, graph, _ = _ready(tmp_path)
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)
    repository.decide(resolution)

    _resume(coordinator, graph, owner="worker-1")

    [(command, config)] = graph.calls
    assert isinstance(command, Command)
    assert command.resume["interrupt-1"] | {"claim_token": None} == (
        resolution.to_payload() | {"claim_token": None}
    )
    assert isinstance(command.resume["interrupt-1"]["claim_token"], str)
    assert config["configurable"]["thread_id"] == "thread-1"
    assert "checkpoint_id" not in config["configurable"]


def test_stateless_or_threadless_approval_fails_closed(tmp_path: Any) -> None:
    calls: list[str] = []
    for context, lifecycle in (
        (CONTEXT, None),
        (
            ToolGovernanceContext("tenant-a", "principal-1", "run-1"),
            SQLiteApprovalRepository(tmp_path / "approvals.sqlite3"),
        ),
    ):
        action = normalize_tool_action(
            name="delete_record",
            arguments={},
            context=context,
            side_effect=SideEffectClass.SIDE_EFFECTING,
        )
        with pytest.raises(ApprovalRequiresThreadError) as raised:
            guard_tool_call(
                action,
                context,
                lambda: calls.append("ran"),
                client=SequencedClient(),
                approval_lifecycle=lifecycle,
                interrupt=Pause(),
            )
        assert raised.value.code == "zeroth.approval_requires_thread"
    assert calls == []


def test_decide_expires_an_overdue_approval_without_reconciliation(tmp_path: Any) -> None:
    now = [10.0]
    repository = SQLiteApprovalRepository(
        tmp_path / "approvals.sqlite3", ttl_seconds=5, clock=lambda: now[0]
    )
    pause = Pause()
    _request(repository, SequencedClient(), pause)
    assert pause.payload is not None
    saver = PersistentSaver(tmp_path / "checkpoints.bin")
    graph = govern_graph(Graph(_snapshot(pause.payload), saver))
    ApprovalCoordinator(repository).confirm_checkpoint(
        "approval-7", graph, config={}, durable_checkpointer=saver
    )

    now[0] = 16.0
    with pytest.raises(ToolGovernanceError):
        repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))

    assert repository.get("approval-7").state is ApprovalState.EXPIRED


def test_checkpoint_confirmation_is_idempotent_after_a_decision(tmp_path: Any) -> None:
    repository, coordinator, graph, _ = _ready(tmp_path)
    repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))

    confirmed = coordinator.confirm_checkpoint(
        "approval-7", graph, config={}, durable_checkpointer=graph.checkpointer
    )

    assert confirmed.state is ApprovalState.DECIDED


def test_resume_targets_the_persisted_interrupt_id_and_preserves_fresh_config(
    tmp_path: Any,
) -> None:
    repository, coordinator, graph, _ = _ready(tmp_path)
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)
    repository.decide(resolution)

    fresh = {"configurable": {"_zeroth": "fresh-token", "thread_id": "attacker-thread"}}
    _resume(coordinator, graph, owner="worker-1", config=fresh)

    [(command, config)] = graph.calls
    assert command.resume["interrupt-1"] | {"claim_token": None} == (
        resolution.to_payload() | {"claim_token": None}
    )
    assert config["configurable"]["_zeroth"] == "fresh-token"
    assert config["configurable"]["thread_id"] == "thread-1"
    assert "checkpoint_id" not in config["configurable"]


@pytest.mark.parametrize("async_mode", [False, True], ids=("sync", "async"))
def test_coordinator_resume_discards_only_the_bound_checkpoint(
    tmp_path: Any, async_mode: bool
) -> None:
    repository, coordinator, graph, _ = _ready(tmp_path)
    repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))
    hook = object()
    resumed_graph = graph.with_config(
        {
            "configurable": {
                "thread_id": "thread-1",
                "__pregel_checkpointer": graph.checkpointer,
                "checkpoint_id": "bound-stale",
                "checkpoint_ns": "bound|stale",
                "checkpoint_map": {"bound|stale": "bound-stale"},
                "bound": "preserved",
            }
        }
    )
    fresh = {
        "configurable": {
            "thread_id": "attacker-thread",
            "__pregel_checkpointer": graph.checkpointer,
            "checkpoint_ns": "caller|stale",
            "checkpoint_map": {"caller|stale": "caller-stale"},
            "_zeroth": "fresh-token",
            "fresh": "preserved",
        },
        "callbacks": [hook],
        "tags": ["resumed"],
    }

    if async_mode:
        asyncio.run(
            coordinator.aresume(
                "approval-7",
                resumed_graph,
                owner="worker-1",
                config=fresh,
                durable_checkpointer=graph.checkpointer,
            )
        )
    else:
        coordinator.resume(
            "approval-7",
            resumed_graph,
            owner="worker-1",
            config=fresh,
            durable_checkpointer=graph.checkpointer,
        )

    [(_command, delivered)] = graph._graph.calls
    configurable = delivered["configurable"]
    assert configurable == {
        "thread_id": "thread-1",
        "bound": "preserved",
        "_zeroth": "fresh-token",
        "fresh": "preserved",
    }
    assert hook in delivered["callbacks"]
    assert delivered["tags"] == ["resumed"]
    assert json.loads(json.dumps(configurable)) == configurable


def test_tool_failure_propagates_and_cannot_be_replayed(tmp_path: Any) -> None:
    repository, coordinator, graph, policy = _ready(tmp_path)
    repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))

    def fail(value: Any) -> None:
        guard_tool_call(
            ACTION,
            CONTEXT,
            lambda: (_ for _ in ()).throw(RuntimeError("tool-failed")),
            client=policy,
            interrupt=lambda _payload: value,
            approval_lifecycle=repository,
        )

    graph._graph.callback = fail
    with pytest.raises(RuntimeError, match="tool-failed"):
        _resume(coordinator, graph, owner="worker-1")

    _resume(coordinator, graph, owner="worker-2")
    assert len(graph.calls) == 1


@pytest.mark.parametrize("async_mode", [False, True], ids=("sync", "async"))
def test_real_parallel_interrupt_resume_ignores_a_bound_stale_checkpoint(
    tmp_path: Any, async_mode: bool
) -> None:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    saver_path = tmp_path / "checkpoints.bin"
    saver = AsyncOnlyPersistentSaver(saver_path) if async_mode else PersistentSaver(saver_path)
    policy = PerToolApprovalClient()
    executed: list[str] = []

    graph = govern_graph(
        _parallel_builder(repository, policy, executed).compile(checkpointer=saver)
    )
    config = {"configurable": {"thread_id": "thread-1"}}

    if async_mode:
        asyncio.run(graph.ainvoke({"done": []}, config))
        initial = asyncio.run(graph.aget_state(config))
    else:
        graph.invoke({"done": []}, config)
        initial = graph.get_state(config)
    reopened = PersistentSaver(saver_path)
    assert reopened.get_tuple(config) is not None
    assert initial.config["configurable"].get("checkpoint_id")
    resumed_graph = graph.with_config(
        {
            **initial.config,
            "configurable": {
                **initial.config["configurable"],
                "checkpoint_ns": "stale|subgraph",
                "checkpoint_map": {"stale|subgraph": "stale-checkpoint"},
            },
        }
    )

    coordinator = ApprovalCoordinator(repository)
    if async_mode:
        first = asyncio.run(
            coordinator.aconfirm_checkpoint(
                "approval-tool_a", resumed_graph, config=config, durable_checkpointer=saver
            )
        )
        second = asyncio.run(
            coordinator.aconfirm_checkpoint(
                "approval-tool_b", resumed_graph, config=config, durable_checkpointer=saver
            )
        )
    else:
        first = coordinator.confirm_checkpoint(
            "approval-tool_a", resumed_graph, config=config, durable_checkpointer=saver
        )
        second = coordinator.confirm_checkpoint(
            "approval-tool_b", resumed_graph, config=config, durable_checkpointer=saver
        )
    assert first.interrupt_id and second.interrupt_id
    assert first.interrupt_id != second.interrupt_id

    repository.decide(ApprovalResolution("approval-tool_a", ApprovalDecision.APPROVE))
    if async_mode:
        asyncio.run(
            coordinator.aresume(
                "approval-tool_a",
                resumed_graph,
                owner="worker-a",
                config=config,
                durable_checkpointer=saver,
            )
        )
    else:
        coordinator.resume(
            "approval-tool_a",
            resumed_graph,
            owner="worker-a",
            config=config,
            durable_checkpointer=saver,
        )

    assert executed == ["tool_a"]
    assert repository.get("approval-tool_a").state is ApprovalState.RESOLVED
    assert repository.get("approval-tool_b").state is ApprovalState.READY
    remaining = asyncio.run(graph.aget_state(config)) if async_mode else graph.get_state(config)
    assert remaining.next == ("tool_b",)
    assert "approval-tool_b" in {item.value["approval_ref"] for item in remaining.interrupts}

    repository.decide(ApprovalResolution("approval-tool_b", ApprovalDecision.APPROVE))
    if async_mode:
        asyncio.run(
            coordinator.aresume(
                "approval-tool_b",
                resumed_graph,
                owner="worker-b",
                config=config,
                durable_checkpointer=saver,
            )
        )
    else:
        coordinator.resume(
            "approval-tool_b",
            resumed_graph,
            owner="worker-b",
            config=config,
            durable_checkpointer=saver,
        )

    assert executed == ["tool_a", "tool_b"]
    assert repository.get("approval-tool_b").state is ApprovalState.RESOLVED
    completed = asyncio.run(graph.aget_state(config)) if async_mode else graph.get_state(config)
    assert completed.next == ()
    assert completed.values["done"] == ["tool_a", "tool_b"]


@pytest.mark.parametrize("async_mode", [False, True], ids=("sync", "async"))
def test_real_effective_checkpointer_override_fails_closed_before_execution(
    tmp_path: Any, async_mode: bool
) -> None:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    durable = PersistentSaver(tmp_path / "durable-checkpoints.bin")
    override = InMemorySaver()
    executed: list[str] = []
    graph = govern_graph(
        _parallel_builder(repository, PerToolApprovalClient(), executed).compile(
            checkpointer=durable
        )
    )
    config = {
        "configurable": {
            "thread_id": "thread-1",
            "__pregel_checkpointer": override,
        }
    }

    if async_mode:
        asyncio.run(graph.ainvoke({"done": []}, config))
    else:
        graph.invoke({"done": []}, config)

    coordinator = ApprovalCoordinator(repository)
    with pytest.raises(ApprovalRequiresThreadError):
        if async_mode:
            asyncio.run(
                coordinator.aconfirm_checkpoint(
                    "approval-tool_a",
                    graph,
                    config=config,
                    durable_checkpointer=durable,
                )
            )
        else:
            coordinator.confirm_checkpoint(
                "approval-tool_a",
                graph,
                config=config,
                durable_checkpointer=durable,
            )

    assert executed == []
    assert repository.get("approval-tool_a").state is ApprovalState.AWAITING_CHECKPOINT
    assert durable.get_tuple({"configurable": {"thread_id": "thread-1"}}) is None


@pytest.mark.parametrize("async_mode", [False, True], ids=("sync", "async"))
def test_real_nested_resume_ignores_stale_observation_coordinates(
    tmp_path: Any, async_mode: bool
) -> None:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    saver_path = tmp_path / "checkpoints.bin"
    saver = AsyncOnlyPersistentSaver(saver_path) if async_mode else PersistentSaver(saver_path)
    policy = PerToolApprovalClient()
    executed: list[str] = []
    action = normalize_tool_action(
        name="nested_tool",
        arguments={"record": "nested"},
        context=CONTEXT,
        side_effect=SideEffectClass.SIDE_EFFECTING,
        tool_call_id="call-nested",
    )

    def run(_state: ParallelState) -> ParallelState:
        guard_tool_call(
            action,
            CONTEXT,
            lambda: executed.append("nested"),
            client=policy,
            approval_lifecycle=repository,
        )
        return {"done": ["nested"]}

    child = StateGraph(ParallelState)
    child.add_node("nested_tool", run)
    child.add_edge(START, "nested_tool")
    child.add_edge("nested_tool", END)
    parent = StateGraph(ParallelState)
    parent.add_node("child", child.compile())
    parent.add_edge(START, "child")
    parent.add_edge("child", END)
    graph = govern_graph(parent.compile(checkpointer=saver))
    config = {"configurable": {"thread_id": "thread-1"}}

    if async_mode:
        asyncio.run(graph.ainvoke({"done": []}, config))
    else:
        graph.invoke({"done": []}, config)

    stale = {
        "configurable": {
            "thread_id": "attacker-thread",
            "checkpoint_id": "stale-checkpoint",
            "checkpoint_ns": "child",
            "checkpoint_map": {"child": "stale-checkpoint"},
        }
    }
    coordinator = ApprovalCoordinator(repository)
    if async_mode:
        asyncio.run(
            coordinator.aconfirm_checkpoint(
                "approval-nested_tool", graph, config=stale, durable_checkpointer=saver
            )
        )
    else:
        coordinator.confirm_checkpoint(
            "approval-nested_tool", graph, config=stale, durable_checkpointer=saver
        )
    repository.decide(ApprovalResolution("approval-nested_tool", ApprovalDecision.APPROVE))
    if async_mode:
        asyncio.run(
            coordinator.aresume(
                "approval-nested_tool",
                graph,
                owner="worker-1",
                config=stale,
                durable_checkpointer=saver,
            )
        )
    else:
        coordinator.resume(
            "approval-nested_tool",
            graph,
            owner="worker-1",
            config=stale,
            durable_checkpointer=saver,
        )

    assert executed == ["nested"]
    assert repository.get("approval-nested_tool").state is ApprovalState.RESOLVED


@pytest.mark.parametrize("async_mode", [False, True], ids=("sync", "async"))
@pytest.mark.parametrize(
    ("resolution", "drift"),
    [
        (ApprovalResolution("approval-7", ApprovalDecision.REJECT), "arguments"),
        (
            ApprovalResolution("approval-7", ApprovalDecision.APPROVE, {"record": "approved"}),
            "arguments",
        ),
        (
            ApprovalResolution("approval-7", ApprovalDecision.APPROVE, {"record": "approved"}),
            "context",
        ),
    ],
    ids=("stored-rejection", "approved-arguments", "approved-context"),
)
def test_real_claimed_resume_rejects_identity_drift_before_policy_or_execution(
    tmp_path: Any,
    async_mode: bool,
    resolution: ApprovalResolution,
    drift: str,
) -> None:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    saver_path = tmp_path / "checkpoints.bin"
    saver = AsyncOnlyPersistentSaver(saver_path) if async_mode else PersistentSaver(saver_path)
    policy = SequencedClient()
    executed: list[str] = []
    live: dict[str, Any] = {"context": CONTEXT, "record": "original"}

    if async_mode:

        async def dangerous(record: str) -> None:
            executed.append(record)

        [governed] = govern_tools(
            [dangerous],
            context=lambda: live["context"],
            client=policy,
            side_effect=lambda _tool: SideEffectClass.SIDE_EFFECTING,
            approval_lifecycle=repository,
        )

        async def run(_state: ParallelState) -> ParallelState:
            await governed(record=live["record"])
            return {"done": ["dangerous"]}

    else:

        def dangerous(record: str) -> None:
            executed.append(record)

        [governed] = govern_tools(
            [dangerous],
            context=lambda: live["context"],
            client=policy,
            side_effect=lambda _tool: SideEffectClass.SIDE_EFFECTING,
            approval_lifecycle=repository,
        )

        def run(_state: ParallelState) -> ParallelState:
            governed(record=live["record"])
            return {"done": ["dangerous"]}

    builder = StateGraph(ParallelState)
    builder.add_node("dangerous", run)
    builder.add_edge(START, "dangerous")
    builder.add_edge("dangerous", END)
    graph = govern_graph(builder.compile(checkpointer=saver))
    config = {"configurable": {"thread_id": "thread-1"}}

    if async_mode:
        asyncio.run(graph.ainvoke({"done": []}, config))
    else:
        graph.invoke({"done": []}, config)
    coordinator = ApprovalCoordinator(repository)
    if async_mode:
        asyncio.run(
            coordinator.aconfirm_checkpoint(
                "approval-7", graph, config=config, durable_checkpointer=saver
            )
        )
    else:
        coordinator.confirm_checkpoint(
            "approval-7", graph, config=config, durable_checkpointer=saver
        )
    repository.decide(resolution)
    if drift == "arguments":
        live["record"] = "drifted"
    else:
        live["context"] = ToolGovernanceContext(
            tenant_id="tenant-a",
            principal_id="principal-1",
            run_id="run-drifted",
            thread_id="thread-1",
        )

    expected_error = (
        PolicyViolation if resolution.decision is ApprovalDecision.REJECT else ToolGovernanceError
    )
    with pytest.raises(expected_error):
        if async_mode:
            asyncio.run(
                coordinator.aresume(
                    "approval-7",
                    graph,
                    owner="worker-1",
                    config=config,
                    durable_checkpointer=saver,
                )
            )
        else:
            coordinator.resume(
                "approval-7",
                graph,
                owner="worker-1",
                config=config,
                durable_checkpointer=saver,
            )

    assert executed == []
    assert policy.actions == [{"record": "original"}]
    expected_state = (
        ApprovalState.RESOLVED
        if resolution.decision is ApprovalDecision.REJECT
        else ApprovalState.ORPHANED
    )
    assert repository.get("approval-7").state is expected_state


@pytest.mark.parametrize("async_mode", [False, True], ids=("sync", "async"))
def test_real_tool_node_keeps_identical_calls_separate_by_outer_id(
    tmp_path: Any, async_mode: bool
) -> None:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    saver_path = tmp_path / "checkpoints.bin"
    saver = AsyncOnlyPersistentSaver(saver_path) if async_mode else PersistentSaver(saver_path)
    policy = IdenticalCallApprovalClient()
    executed: list[str] = []

    def lookup(query: str) -> str:
        executed.append(query)
        return query

    async def alookup(query: str) -> str:
        executed.append(query)
        return query

    original = StructuredTool.from_function(
        func=lookup,
        coroutine=alookup,
        name="lookup",
        description="Look up a record.",
    )
    assert set(original.args_schema.model_fields) == {"query"}
    [governed] = govern_tools(
        [original],
        context=CONTEXT,
        client=policy,
        side_effect=lambda _tool: SideEffectClass.READ_ONLY,
        approval_lifecycle=repository,
    )
    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([governed]))
    builder.add_conditional_edges(
        START,
        lambda state: [
            Send("tools", [tool_call]) for tool_call in state["messages"][-1].tool_calls
        ],
    )
    builder.add_edge("tools", END)
    graph = govern_graph(builder.compile(checkpointer=saver))
    config = {"configurable": {"thread_id": "thread-1"}}
    calls = [
        {"name": "lookup", "args": {"query": "same"}, "id": call_id}
        for call_id in ("call-a", "call-b")
    ]

    if async_mode:
        asyncio.run(graph.ainvoke({"messages": [AIMessage(content="", tool_calls=calls)]}, config))
        initial = asyncio.run(graph.aget_state(config))
    else:
        graph.invoke({"messages": [AIMessage(content="", tool_calls=calls)]}, config)
        initial = graph.get_state(config)

    assert executed == []
    assert len(initial.interrupts) == 2
    assert {item.value["tool_call_id"] for item in initial.interrupts} == {
        "call-a",
        "call-b",
    }
    pending = repository.pending()
    assert len(pending) == 2
    assert {item.intent.payload["tool_call_id"] for item in pending} == {
        "call-a",
        "call-b",
    }

    policy.hold = False
    resumed_graph = graph.with_config(initial.config)
    coordinator = ApprovalCoordinator(repository)
    refs = [str(item.intent.payload["approval_ref"]) for item in pending]
    for ref in refs:
        if async_mode:
            asyncio.run(
                coordinator.aconfirm_checkpoint(
                    ref, resumed_graph, config=config, durable_checkpointer=saver
                )
            )
        else:
            coordinator.confirm_checkpoint(
                ref, resumed_graph, config=config, durable_checkpointer=saver
            )
        repository.decide(ApprovalResolution(ref, ApprovalDecision.APPROVE))

    for index, ref in enumerate(refs, start=1):
        if async_mode:
            asyncio.run(
                coordinator.aresume(
                    ref,
                    resumed_graph,
                    owner=f"worker-{index}",
                    config=config,
                    durable_checkpointer=saver,
                )
            )
        else:
            coordinator.resume(
                ref,
                resumed_graph,
                owner=f"worker-{index}",
                config=config,
                durable_checkpointer=saver,
            )
        assert len(executed) == index
        assert repository.get(ref).state is ApprovalState.RESOLVED

    completed = asyncio.run(graph.aget_state(config)) if async_mode else graph.get_state(config)
    outputs = [
        message for message in completed.values["messages"] if isinstance(message, ToolMessage)
    ]
    assert {message.tool_call_id for message in outputs} == {"call-a", "call-b"}
    assert executed == ["same", "same"]


def test_parallel_resumes_are_serialized_across_repository_instances(tmp_path: Any) -> None:
    path = tmp_path / "approvals.sqlite3"
    repository = SQLiteApprovalRepository(path)
    saver = PersistentSaver(tmp_path / "checkpoints.bin")
    policy = PerToolApprovalClient()
    executed: list[str] = []
    compiled = _parallel_builder(repository, policy, executed).compile(checkpointer=saver)
    probe = ResumeConcurrencyProbe(compiled, saver)
    graph = govern_graph(probe)
    config = {"configurable": {"thread_id": "thread-1"}}
    graph.invoke({"done": []}, config)
    first = ApprovalCoordinator(repository)
    second = ApprovalCoordinator(SQLiteApprovalRepository(path))
    first.confirm_checkpoint("approval-tool_a", graph, config=config, durable_checkpointer=saver)
    second.confirm_checkpoint("approval-tool_b", graph, config=config, durable_checkpointer=saver)
    repository.decide(ApprovalResolution("approval-tool_a", ApprovalDecision.APPROVE))
    repository.decide(ApprovalResolution("approval-tool_b", ApprovalDecision.APPROVE))
    probe.coordinate = True

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(
                first.resume,
                "approval-tool_a",
                graph,
                owner="worker-a",
                config=config,
                durable_checkpointer=saver,
            ),
            pool.submit(
                second.resume,
                "approval-tool_b",
                graph,
                owner="worker-b",
                config=config,
                durable_checkpointer=saver,
            ),
        )
        records = [future.result() for future in futures]

    assert all(record.state is ApprovalState.RESOLVED for record in records)
    assert sorted(executed) == ["tool_a", "tool_b"]
    assert probe.max_active == 1
    completed = graph.get_state(config)
    assert sorted(completed.values["done"]) == ["tool_a", "tool_b"]


def test_real_governed_gateway_resume_uses_fresh_reserved_context(tmp_path: Any) -> None:
    repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
    saver_path = tmp_path / "checkpoints.bin"
    saver = PersistentSaver(saver_path)
    executed: list[int] = []
    events: list[tuple[str, dict[str, Any]]] = []
    codec = ReservedContextCodec(_signer(), clock=lambda: 150)
    initial_token = _token(codec, "initial", principal_id="principal-1")
    fresh_token = _token(codec, "fresh", principal_id="principal-1")
    gateway_context = ToolGovernanceContext(
        tenant_id="tenant-a",
        principal_id="principal-1",
        run_id="run-1",
        thread_id="thread-1",
        correlation_id="approval-flow",
    )
    action = normalize_tool_action(
        name="delete_record",
        arguments={"table": "invoices", "id": 41},
        context=gateway_context,
        side_effect=SideEffectClass.SIDE_EFFECTING,
        tool_call_id="call-1",
    )
    inventory = ToolInventory(
        entries=(
            ToolInventoryEntry(
                identity=action.identity,
                side_effect=SideEffectClass.SIDE_EFFECTING,
                requires_approval=True,
            ),
        ),
        coverage=InventoryCoverage.COMPLETE,
    )
    decision_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal decision_count
        payload = json.loads(request.content)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        events.append((endpoint, payload))
        if endpoint in {"inventories", "heartbeat"}:
            return httpx.Response(204)
        if endpoint == "attestations":
            return httpx.Response(
                200,
                json={
                    "correlation_id": payload["correlation_id"],
                    "governance_level": payload["claimed_level"],
                    "observed_at": "2026-08-05T00:00:00Z",
                    "graph_version": payload["graph_version"],
                    "adapter_version": payload["adapter_version"],
                    "inventory_fingerprint": payload["inventory_fingerprint"],
                    "signature_valid": True,
                    "tool_manifest_complete": True,
                },
            )
        decision_count += 1
        decision = "require_approval" if decision_count == 1 else "allow"
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "decision_id": f"decision-{decision_count}",
                "idempotency_key": payload["idempotency_key"],
                "decision": decision,
                "reason_code": "approval_required" if decision_count <= 2 else "allowed",
                "policy_version": "policy-v1",
                "approval_ref": "approval-7" if decision_count <= 2 else None,
            },
        )

    gateway = LangGraphGatewayClient(
        "https://zeroth.test",
        api_key="secret",
        tenant_id="tenant-a",
        principal_id="principal-1",
        deployment_ref="deployment-a",
        policy_version="policy-v1",
        graph_version="graph-v1",
        inventory=inventory,
        heartbeat_interval_seconds=None,
        transport=httpx.MockTransport(handler),
    )

    def run(_state: ParallelState) -> ParallelState:
        guard_tool_call(
            action,
            gateway_context,
            lambda: executed.append(41),
            client=gateway,
            approval_lifecycle=repository,
        )
        return {"done": ["lookup"]}

    builder = StateGraph(ParallelState)
    builder.add_node("lookup", run)
    builder.add_edge(START, "lookup")
    builder.add_edge("lookup", END)
    graph = govern_graph(builder.compile(checkpointer=saver), gateway_client=gateway)
    initial_config = {"configurable": {"thread_id": "thread-1", "_zeroth": initial_token}}
    fresh_config = {
        "configurable": {"thread_id": "untrusted", "_zeroth": fresh_token},
        "tags": ["resumed"],
    }
    try:
        graph.invoke({"done": []}, initial_config)
        coordinator = ApprovalCoordinator(repository)
        coordinator.confirm_checkpoint(
            "approval-7", graph, config=initial_config, durable_checkpointer=saver
        )
        repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))
        coordinator.resume(
            "approval-7",
            graph,
            owner="approval-worker",
            config=fresh_config,
            durable_checkpointer=saver,
        )
    finally:
        gateway.close()

    assert executed == [41]
    assert repository.get("approval-7").state is ApprovalState.RESOLVED
    decisions = [payload for endpoint, payload in events if endpoint == "decisions"]
    assert [payload["context_token"] for payload in decisions] == [
        initial_token,
        fresh_token,
    ]
    assert [endpoint for endpoint, _payload in events].count("inventories") == 2
    assert [endpoint for endpoint, _payload in events].count("attestations") == 2
    assert (
        PersistentSaver(saver_path).get_tuple({"configurable": {"thread_id": "thread-1"}})
        is not None
    )


def test_expired_unconsumed_lease_is_reclaimed_with_a_new_execution_fence(
    tmp_path: Any,
) -> None:
    now = [0.0]
    repository = SQLiteApprovalRepository(
        tmp_path / "approvals.sqlite3",
        ttl_seconds=100,
        lease_seconds=1,
        clock=lambda: now[0],
    )
    pause = Pause()
    _request(repository, SequencedClient(), pause)
    assert pause.payload is not None
    saver = PersistentSaver(tmp_path / "checkpoints.bin")
    calls: list[str] = []
    tokens: list[str] = []

    class BlockingGraph(Graph):
        def invoke(self, command: Any, config: dict[str, Any]) -> None:
            del config
            delivery = command.resume["interrupt-1"]
            tokens.append(delivery["claim_token"])
            consumed = repository.consume(delivery)
            calls.append(consumed.claim_token)
            repository.finish("approval-7", consumed.claim_token)
            self.state = _snapshot(None, checkpoint_id="checkpoint-2")

    graph = govern_graph(BlockingGraph(_snapshot(pause.payload), saver))
    coordinator = ApprovalCoordinator(repository)
    coordinator.confirm_checkpoint("approval-7", graph, config={}, durable_checkpointer=saver)
    repository.decide(ApprovalResolution("approval-7", ApprovalDecision.APPROVE))
    crashed = repository.claim("approval-7", owner="crashed-worker")
    assert crashed.claim_token is not None

    now[0] = 2.0
    _resume(coordinator, graph, owner="replacement-worker")

    assert len(tokens) == 1 and tokens[0] != crashed.claim_token
    assert calls == tokens
    assert repository.get("approval-7").state is ApprovalState.RESOLVED


def test_claim_and_consume_atomically_refuse_overdue_approvals(tmp_path: Any) -> None:
    now = [10.0]
    repository = SQLiteApprovalRepository(
        tmp_path / "approvals.sqlite3", ttl_seconds=5, clock=lambda: now[0]
    )
    pause = Pause()
    _request(repository, SequencedClient(), pause)
    assert pause.payload is not None
    saver = PersistentSaver(tmp_path / "checkpoints.bin")
    graph = govern_graph(Graph(_snapshot(pause.payload), saver))
    coordinator = ApprovalCoordinator(repository)
    coordinator.confirm_checkpoint("approval-7", graph, config={}, durable_checkpointer=saver)
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)
    repository.decide(resolution)

    now[0] = 16.0
    with pytest.raises(ToolGovernanceError, match="deadline"):
        _resume(coordinator, graph, owner="late-worker")
    assert repository.get("approval-7").state is ApprovalState.EXPIRED
    assert graph._graph.calls == []

    consume_repository = SQLiteApprovalRepository(
        tmp_path / "consume.sqlite3", ttl_seconds=5, clock=lambda: now[0]
    )
    now[0] = 20.0
    consume_pause = Pause()
    _request(consume_repository, SequencedClient(), consume_pause)
    assert consume_pause.payload is not None
    consume_repository.ready("approval-7", "checkpoint-1", "interrupt-1")
    consume_repository.decide(resolution)
    claimed = consume_repository.claim("approval-7", owner="worker")
    assert claimed.claim_token is not None
    now[0] = 26.0
    with pytest.raises(ToolGovernanceError, match="deadline"):
        consume_repository.consume({**resolution.to_payload(), "claim_token": claimed.claim_token})
    assert consume_repository.get("approval-7").state is ApprovalState.EXPIRED


def test_consumed_claim_can_finish_after_the_request_deadline(tmp_path: Any) -> None:
    now = [10.0]
    repository = SQLiteApprovalRepository(
        tmp_path / "approvals.sqlite3", ttl_seconds=5, clock=lambda: now[0]
    )
    pause = Pause()
    _request(repository, SequencedClient(), pause)
    repository.ready("approval-7", "checkpoint-1", "interrupt-1")
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)
    repository.decide(resolution)
    claimed = repository.claim("approval-7", owner="worker")
    assert claimed.claim_token is not None
    [stale_pending] = repository.pending()
    repository.consume({**resolution.to_payload(), "claim_token": claimed.claim_token})

    now[0] = 16.0
    repository.pending = lambda limit=100: (stale_pending,)  # type: ignore[method-assign]
    assert repository.expire_due() == ()
    assert repository.decide(resolution).state is ApprovalState.RESUMING
    assert repository.finish("approval-7", claimed.claim_token).state is ApprovalState.RESOLVED


def test_checkpoint_confirmation_requires_the_attested_durable_saver(
    tmp_path: Any,
) -> None:
    class BrokenGraph(Graph):
        def get_state(self, config: dict[str, Any]) -> StateSnapshot:
            del config
            raise RuntimeError("checkpoint backend unavailable")

    for case in ("missing", "memory", "mismatch", "broken"):
        directory = tmp_path / case
        directory.mkdir()
        repository = SQLiteApprovalRepository(directory / "approvals.sqlite3")
        pause = Pause()
        _request(repository, SequencedClient(), pause)
        assert pause.payload is not None
        persistent = PersistentSaver(directory / "checkpoints.bin")
        if case == "missing":
            raw: Any = Graph(_snapshot(pause.payload), None)  # type: ignore[arg-type]
            attested: Any = None
        elif case == "memory":
            memory = InMemorySaver()
            raw = Graph(_snapshot(pause.payload), memory)
            attested = memory
        elif case == "mismatch":
            raw = Graph(_snapshot(pause.payload), persistent)
            attested = PersistentSaver(directory / "other-checkpoints.bin")
        else:
            raw = BrokenGraph(_snapshot(pause.payload), persistent)
            attested = persistent
        graph = govern_graph(raw)

        with pytest.raises(ApprovalRequiresThreadError) as raised:
            ApprovalCoordinator(repository).confirm_checkpoint(
                "approval-7", graph, config={}, durable_checkpointer=attested
            )

        assert raised.value.code == "zeroth.approval_requires_thread"
        assert repository.get("approval-7").state is ApprovalState.AWAITING_CHECKPOINT


def test_checkpoint_confirmation_is_idempotent_through_all_progressed_states(
    tmp_path: Any,
) -> None:
    repository, coordinator, graph, _policy = _ready(tmp_path)
    assert (
        coordinator.confirm_checkpoint(
            "approval-7", graph, config={}, durable_checkpointer=graph.checkpointer
        ).state
        is ApprovalState.READY
    )
    resolution = ApprovalResolution("approval-7", ApprovalDecision.APPROVE)
    repository.decide(resolution)
    assert (
        coordinator.confirm_checkpoint(
            "approval-7", graph, config={}, durable_checkpointer=graph.checkpointer
        ).state
        is ApprovalState.DECIDED
    )
    claimed = repository.claim("approval-7", owner="worker")
    assert claimed.claim_token is not None
    assert (
        coordinator.confirm_checkpoint(
            "approval-7", graph, config={}, durable_checkpointer=graph.checkpointer
        ).state
        is ApprovalState.RESUMING
    )
    repository.consume({**resolution.to_payload(), "claim_token": claimed.claim_token})
    repository.finish("approval-7", claimed.claim_token)
    graph._graph.state = _snapshot(None, checkpoint_id="checkpoint-2")
    assert (
        coordinator.confirm_checkpoint(
            "approval-7", graph, config={}, durable_checkpointer=graph.checkpointer
        ).state
        is ApprovalState.RESOLVED
    )


@pytest.mark.parametrize(
    ("resolution", "fresh", "decision_term", "approval_action", "actual_arguments"),
    [
        (
            ApprovalResolution(
                "approval-7", ApprovalDecision.APPROVE, {"table": "invoices", "id": 42}
            ),
            ALLOW,
            "approve",
            "approve",
            {"table": "invoices", "id": 42},
        ),
        (
            ApprovalResolution("approval-7", ApprovalDecision.REJECT),
            ALLOW,
            "reject",
            "reject",
            {"table": "invoices", "id": 41},
        ),
        (
            ApprovalResolution(
                "approval-7", ApprovalDecision.APPROVE, {"table": "invoices", "id": 43}
            ),
            DENY,
            "deny",
            "approve",
            {"table": "invoices", "id": 43},
        ),
    ],
)
def test_approval_audit_is_deduplicated_and_records_the_final_actual_call(
    tmp_path: Any,
    resolution: ApprovalResolution,
    fresh: ToolDecision,
    decision_term: str,
    approval_action: str,
    actual_arguments: dict[str, Any],
) -> None:
    audit = RecordingSubmitter()
    policy = SequencedClient(fresh=fresh)
    repository, coordinator, graph, _policy = _ready(tmp_path, policy, audit=audit)

    def resume(value: Any) -> None:
        guard_tool_call(
            ACTION,
            CONTEXT,
            lambda: None,
            client=policy,
            interrupt=lambda _payload: value,
            approval_lifecycle=repository,
            invoke_with_arguments=lambda _arguments: None,
            audit=audit,
        )

    graph._graph.callback = resume
    repository.decide(resolution)
    with contextlib.suppress(PolicyViolation):
        _resume(coordinator, graph, owner="worker")

    assert len(audit.records) == 2
    requested, final = audit.records
    assert requested.execution_metadata["decision"] == "require_approval"
    assert requested.approval_actions[0].action == "requested"
    assert final.execution_metadata["decision"] == decision_term
    assert final.approval_actions[0].action == approval_action
    assert final.tool_calls[0].arguments == actual_arguments


def test_async_coordinator_uses_only_async_state_and_invoke_paths(tmp_path: Any) -> None:
    async def scenario() -> None:
        repository = SQLiteApprovalRepository(tmp_path / "approvals.sqlite3")
        saver = AsyncOnlyPersistentSaver(tmp_path / "checkpoints.bin")
        policy = PerToolApprovalClient()
        executed: list[str] = []
        action = normalize_tool_action(
            name="async_tool",
            arguments={"record": "original"},
            context=CONTEXT,
            side_effect=SideEffectClass.SIDE_EFFECTING,
            tool_call_id="call-async",
        )

        async def run(_state: ParallelState) -> ParallelState:
            async def execute() -> None:
                executed.append("original")

            async def execute_edited(arguments: Mapping[str, Any]) -> None:
                executed.append(str(arguments["record"]))

            await aguard_tool_call(
                action,
                CONTEXT,
                execute,
                client=policy,
                approval_lifecycle=repository,
                invoke_with_arguments=execute_edited,
            )
            return {"done": [executed[-1]]}

        builder = StateGraph(ParallelState)
        builder.add_node("async_tool", run)
        builder.add_edge(START, "async_tool")
        builder.add_edge("async_tool", END)
        graph = govern_graph(builder.compile(checkpointer=saver))
        config = {"configurable": {"thread_id": "thread-1"}}
        await graph.ainvoke({"done": []}, config)
        coordinator = ApprovalCoordinator(repository)
        await coordinator.aconfirm_checkpoint(
            "approval-async_tool",
            graph,
            config=config,
            durable_checkpointer=saver,
        )
        repository.decide(
            ApprovalResolution(
                "approval-async_tool",
                ApprovalDecision.APPROVE,
                {"record": "edited"},
            )
        )

        record = await coordinator.aresume(
            "approval-async_tool",
            graph,
            owner="async-worker",
            config={
                "configurable": {"thread_id": "attacker-thread"},
                "tags": ["fresh"],
            },
            durable_checkpointer=saver,
        )

        assert record.state is ApprovalState.RESOLVED
        assert executed == ["edited"]
        assert policy.calls == {"async_tool": 2}
        completed = await graph.aget_state(config)
        assert completed.next == ()
        assert completed.values["done"] == ["edited"]

    asyncio.run(scenario())
