"""Durable, fail-closed approval state for governed LangGraph tool calls."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from zeroth.integrations.langgraph._tool_errors import (
    ApprovalRequiresThreadError,
    ToolGovernanceError,
)

_VERSION = 1
_RESOLUTION_KIND = "tool_approval_resolution"
_TERMINAL = ("resolved", "expired", "orphaned")


class ApprovalState(StrEnum):
    """Persisted lifecycle states for one approval."""

    AWAITING_CHECKPOINT = "awaiting_checkpoint"
    READY = "ready"
    DECIDED = "decided"
    RESUMING = "resuming"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    ORPHANED = "orphaned"


class ApprovalDecision(StrEnum):
    """Human answers accepted by the lifecycle."""

    APPROVE = "approve"
    REJECT = "reject"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if type(value) not in (dict, MappingProxyType):
        raise ToolGovernanceError(f"{label} must be a plain mapping")
    try:
        copied = json.loads(json.dumps(dict(value), sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ToolGovernanceError(f"{label} must be JSON serializable") from error
    return MappingProxyType(copied)


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ToolGovernanceError(f"{label} must be a non-blank string")
    return value.strip()


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class ApprovalIntent:
    """Frozen v1 request persisted before LangGraph interrupts."""

    payload: Mapping[str, Any]
    arguments: Mapping[str, Any]
    deadline: float
    version: int = _VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != _VERSION:
            raise ToolGovernanceError("unsupported approval intent version")
        object.__setattr__(self, "payload", _mapping(self.payload, "approval payload"))
        object.__setattr__(self, "arguments", _mapping(self.arguments, "tool arguments"))


@dataclass(frozen=True, slots=True)
class ApprovalResolution:
    """Frozen v1 decision, optionally carrying replacement arguments."""

    approval_ref: str
    decision: ApprovalDecision
    arguments: Mapping[str, Any] | None = None
    version: int = _VERSION

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != _VERSION
            or type(self.decision) is not ApprovalDecision
        ):
            raise ToolGovernanceError("invalid approval resolution")
        object.__setattr__(self, "approval_ref", _identifier(self.approval_ref, "approval ref"))
        if self.arguments is not None:
            object.__setattr__(self, "arguments", _mapping(self.arguments, "edited arguments"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "kind": _RESOLUTION_KIND,
            "approval_ref": self.approval_ref,
            "decision": self.decision.value,
            "arguments": None if self.arguments is None else dict(self.arguments),
        }

    @classmethod
    def from_payload(cls, value: object) -> ApprovalResolution:
        if type(value) is not dict or value.get("kind") != _RESOLUTION_KIND:
            raise ToolGovernanceError("invalid approval resolution payload")
        try:
            decision = ApprovalDecision(value.get("decision"))
        except (TypeError, ValueError) as error:
            raise ToolGovernanceError("invalid approval resolution decision") from error
        return cls(
            value.get("approval_ref"), decision, value.get("arguments"), value.get("version")
        )


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One durable approval row."""

    intent: ApprovalIntent
    state: ApprovalState
    resolution: ApprovalResolution | None
    checkpoint_id: str | None
    owner: str | None
    lease_deadline: float | None


@dataclass(frozen=True, slots=True)
class ApprovalTransition:
    """One accepted or refused lifecycle transition."""

    from_state: ApprovalState | None
    to_state: ApprovalState
    accepted: bool
    occurred_at: float


class SQLiteApprovalRepository:
    """SQLite state machine for approval delivery and restart recovery."""

    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: float = 3600,
        lease_seconds: float = 30,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if str(path) in ("", ":memory:"):
            raise ApprovalRequiresThreadError("approval needs a durable lifecycle store")
        if ttl_seconds <= 0 or lease_seconds <= 0:
            raise ToolGovernanceError("approval deadlines must be positive")
        self._path = str(Path(path))
        self._ttl = float(ttl_seconds)
        self._lease = float(lease_seconds)
        self._clock = clock
        with self._connect() as connection:
            connection.executescript(
                """CREATE TABLE IF NOT EXISTS langgraph_approval_lifecycle (
                approval_ref TEXT PRIMARY KEY, state TEXT NOT NULL, intent TEXT NOT NULL,
                resolution TEXT, checkpoint_id TEXT, owner TEXT, lease_deadline REAL,
                deadline REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS langgraph_approval_events (
                id INTEGER PRIMARY KEY, approval_ref TEXT NOT NULL, from_state TEXT,
                to_state TEXT NOT NULL, accepted INTEGER NOT NULL, occurred_at REAL NOT NULL);"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _intent(self, data: str, deadline: float) -> ApprovalIntent:
        value = json.loads(data)
        return ApprovalIntent(value["payload"], value["arguments"], deadline, value["version"])

    def _record(self, row: sqlite3.Row) -> ApprovalRecord:
        resolution = row["resolution"]
        return ApprovalRecord(
            self._intent(row["intent"], row["deadline"]),
            ApprovalState(row["state"]),
            None if resolution is None else ApprovalResolution.from_payload(json.loads(resolution)),
            row["checkpoint_id"],
            row["owner"],
            row["lease_deadline"],
        )

    def get(self, approval_ref: str) -> ApprovalRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?",
                (approval_ref,),
            ).fetchone()
        if row is None:
            raise ToolGovernanceError("approval lifecycle record does not exist")
        return self._record(row)

    def events(self, approval_ref: str) -> tuple[ApprovalTransition, ...]:
        """Return the durable transition history for one approval."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT from_state, to_state, accepted, occurred_at "
                "FROM langgraph_approval_events WHERE approval_ref = ? ORDER BY id",
                (approval_ref,),
            ).fetchall()
        return tuple(
            ApprovalTransition(
                None if row["from_state"] is None else ApprovalState(row["from_state"]),
                ApprovalState(row["to_state"]),
                bool(row["accepted"]),
                row["occurred_at"],
            )
            for row in rows
        )

    def _event(
        self,
        connection: sqlite3.Connection,
        ref: str,
        source: ApprovalState | None,
        target: ApprovalState,
        accepted: bool,
    ) -> None:
        connection.execute(
            "INSERT INTO langgraph_approval_events VALUES (NULL, ?, ?, ?, ?, ?)",
            (ref, None if source is None else source.value, target.value, accepted, self._clock()),
        )

    def begin(self, payload: Mapping[str, Any], arguments: Mapping[str, Any]) -> ApprovalRecord:
        payload = _mapping(payload, "approval payload")
        ref = _identifier(payload.get("approval_ref"), "approval ref")
        _identifier(payload.get("thread_id"), "thread id")
        _identifier(payload.get("run_id"), "run id")
        intent = ApprovalIntent(payload, arguments, self._clock() + self._ttl)
        encoded = _dump(
            {
                "version": intent.version,
                "payload": dict(intent.payload),
                "arguments": dict(intent.arguments),
            }
        )
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO langgraph_approval_lifecycle "
                "VALUES (?, ?, ?, NULL, NULL, NULL, NULL, ?)",
                (ref, ApprovalState.AWAITING_CHECKPOINT.value, encoded, intent.deadline),
            )
            if cursor.rowcount:
                self._event(connection, ref, None, ApprovalState.AWAITING_CHECKPOINT, True)
        current = self.get(ref)
        if current.intent.payload != intent.payload or current.intent.arguments != intent.arguments:
            with self._connect() as connection:
                self._event(
                    connection, ref, current.state, ApprovalState.AWAITING_CHECKPOINT, False
                )
            raise ToolGovernanceError("approval intent conflicts with its durable record")
        return current

    def _transition(
        self,
        ref: str,
        expected: tuple[ApprovalState, ...],
        target: ApprovalState,
        *,
        checkpoint_id: str | None = None,
        resolution: ApprovalResolution | None = None,
        owner: str | None = None,
        lease_deadline: float | None = None,
    ) -> ApprovalRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                raise ToolGovernanceError("approval lifecycle record does not exist")
            current = self._record(row)
            if current.state is target and (
                target is not ApprovalState.READY or current.checkpoint_id == checkpoint_id
            ):
                return current
            if current.state not in expected:
                self._event(connection, ref, current.state, target, False)
                connection.commit()
                raise ToolGovernanceError(
                    f"approval cannot transition from {current.state.value} to {target.value}"
                )
            connection.execute(
                """UPDATE langgraph_approval_lifecycle
                SET state = ?, resolution = COALESCE(?, resolution),
                checkpoint_id = COALESCE(?, checkpoint_id), owner = ?, lease_deadline = ?
                WHERE approval_ref = ?""",
                (
                    target.value,
                    None if resolution is None else _dump(resolution.to_payload()),
                    checkpoint_id,
                    owner,
                    lease_deadline,
                    ref,
                ),
            )
            self._event(connection, ref, current.state, target, True)
        return self.get(ref)

    def ready(self, ref: str, checkpoint_id: str) -> ApprovalRecord:
        current = self.get(ref)
        if current.state is ApprovalState.READY and current.checkpoint_id == checkpoint_id:
            return current
        return self._transition(
            ref,
            (ApprovalState.AWAITING_CHECKPOINT,),
            ApprovalState.READY,
            checkpoint_id=_identifier(checkpoint_id, "checkpoint id"),
        )

    def decide(self, resolution: ApprovalResolution) -> ApprovalRecord:
        ref = resolution.approval_ref
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                raise ToolGovernanceError("approval lifecycle record does not exist")
            current = self._record(row)
            if current.resolution == resolution:
                return current
            if current.resolution is not None or current.state is not ApprovalState.READY:
                self._event(connection, ref, current.state, ApprovalState.DECIDED, False)
                connection.commit()
                raise ToolGovernanceError("approval cannot accept this decision")
            connection.execute(
                "UPDATE langgraph_approval_lifecycle SET state = ?, resolution = ? "
                "WHERE approval_ref = ?",
                (ApprovalState.DECIDED.value, _dump(resolution.to_payload()), ref),
            )
            self._event(connection, ref, current.state, ApprovalState.DECIDED, True)
        return self.get(ref)

    def _claim(self, ref: str, *, owner: str) -> tuple[ApprovalRecord, bool]:
        owner = _identifier(owner, "resume owner")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                raise ToolGovernanceError("approval lifecycle record does not exist")
            current, now = self._record(row), self._clock()
            if current.state.value in _TERMINAL:
                return current, False
            if (
                current.state is ApprovalState.RESUMING
                and current.lease_deadline is not None
                and current.lease_deadline > now
            ):
                return current, False
            if current.state not in (ApprovalState.DECIDED, ApprovalState.RESUMING):
                self._event(connection, ref, current.state, ApprovalState.RESUMING, False)
                connection.commit()
                raise ToolGovernanceError("approval is not claimable")
            connection.execute(
                "UPDATE langgraph_approval_lifecycle "
                "SET state = ?, owner = ?, lease_deadline = ? WHERE approval_ref = ?",
                (ApprovalState.RESUMING.value, owner, now + self._lease, ref),
            )
            self._event(connection, ref, current.state, ApprovalState.RESUMING, True)
        return self.get(ref), True

    def claim(self, ref: str, *, owner: str) -> ApprovalRecord:
        """Claim resumable work, returning the current owner on duplicate delivery."""
        return self._claim(ref, owner=owner)[0]

    def finish(self, ref: str) -> ApprovalRecord:
        current = self.get(ref)
        return (
            current
            if current.state is ApprovalState.RESOLVED
            else self._transition(ref, (ApprovalState.RESUMING,), ApprovalState.RESOLVED)
        )

    def terminal(self, ref: str, state: ApprovalState) -> ApprovalRecord:
        if state not in (ApprovalState.EXPIRED, ApprovalState.ORPHANED):
            raise ToolGovernanceError("invalid terminal approval state")
        current = self.get(ref)
        if current.state is state:
            return current
        active = tuple(item for item in ApprovalState if item.value not in _TERMINAL)
        return self._transition(ref, active, state)

    def pending(self, limit: int = 100) -> tuple[ApprovalRecord, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ToolGovernanceError("approval pending limit must be between 1 and 1000")
        marks = ",".join("?" for _ in _TERMINAL)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM langgraph_approval_lifecycle "
                f"WHERE state NOT IN ({marks}) ORDER BY deadline LIMIT ?",
                (*_TERMINAL, limit),
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def expire_due(self, limit: int = 100) -> tuple[ApprovalRecord, ...]:
        due = [record for record in self.pending(limit) if record.intent.deadline <= self._clock()]
        return tuple(
            self._transition(ref, (record.state,), ApprovalState.EXPIRED)
            for record in due
            if (ref := _identifier(record.intent.payload.get("approval_ref"), "approval ref"))
        )

    def consume(self, value: object) -> ApprovalResolution:
        resolution = ApprovalResolution.from_payload(value)
        record = self.get(resolution.approval_ref)
        if record.state is not ApprovalState.RESUMING or record.resolution != resolution:
            raise ToolGovernanceError("approval resume does not match the claimed decision")
        return resolution


def _langgraph_command(resume: Mapping[str, Any]) -> Any:
    from langgraph.types import Command

    return Command(resume=dict(resume))


class ApprovalCoordinator:
    """Confirm checkpoints and deliver decisions to their original threads."""

    def __init__(self, repository: SQLiteApprovalRepository) -> None:
        self._repository = repository

    def _observed(self, record: ApprovalRecord, graph: Any) -> tuple[str | None, bool]:
        thread = _identifier(record.intent.payload.get("thread_id"), "thread id")
        snapshot = graph.get_state({"configurable": {"thread_id": thread}})
        config = getattr(snapshot, "config", None)
        configurable = config.get("configurable") if type(config) is dict else None
        checkpoint = configurable.get("checkpoint_id") if type(configurable) is dict else None
        valid = type(configurable) is dict and configurable.get("thread_id") == thread
        interrupts = getattr(snapshot, "interrupts", ())
        matches = type(interrupts) is tuple and any(
            getattr(item, "value", None) == record.intent.payload for item in interrupts
        )
        return checkpoint if valid and type(checkpoint) is str else None, matches

    def confirm_checkpoint(self, ref: str, graph: Any) -> ApprovalRecord:
        record = self._repository.get(ref)
        checkpoint, matches = self._observed(record, graph)
        if checkpoint is None or not matches:
            self._repository.terminal(ref, ApprovalState.ORPHANED)
            raise ToolGovernanceError("approval interrupt is not present in the original thread")
        return self._repository.ready(ref, checkpoint)

    def resume(self, ref: str, graph: Any, *, owner: str) -> ApprovalRecord:
        record = self._repository.get(ref)
        if record.state.value in _TERMINAL:
            return record
        checkpoint, matches = self._observed(record, graph)
        if record.state is ApprovalState.RESUMING and (
            not matches or checkpoint != record.checkpoint_id
        ):
            return self._repository.finish(ref)
        if record.state not in (ApprovalState.DECIDED, ApprovalState.RESUMING):
            raise ToolGovernanceError("approval is not ready to resume")
        if not matches or checkpoint != record.checkpoint_id:
            self._repository.terminal(ref, ApprovalState.ORPHANED)
            raise ToolGovernanceError("approval checkpoint changed before resume")
        claimed, acquired = self._repository._claim(ref, owner=owner)
        if not acquired:
            return claimed
        resolution = claimed.resolution
        if resolution is None:
            raise ToolGovernanceError("claimed approval has no decision")
        thread = _identifier(claimed.intent.payload.get("thread_id"), "thread id")
        config = {"configurable": {"thread_id": thread, "checkpoint_id": claimed.checkpoint_id}}
        try:
            graph.invoke(_langgraph_command(resolution.to_payload()), config)
        except Exception:
            current = self._repository.get(ref)
            if current.state is ApprovalState.RESOLVED:
                return current
            raise
        return self._repository.finish(ref)
