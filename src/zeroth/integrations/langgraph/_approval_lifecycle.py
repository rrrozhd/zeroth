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
from uuid import uuid4

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
class _ApprovalDelivery:
    """One fenced delivery of a persisted approval resolution."""

    resolution: ApprovalResolution
    claim_token: str

    def to_payload(self) -> dict[str, Any]:
        return {**self.resolution.to_payload(), "claim_token": self.claim_token}

    @classmethod
    def from_payload(cls, value: object) -> _ApprovalDelivery:
        if type(value) is not dict:
            raise ToolGovernanceError("invalid approval delivery payload")
        return cls(
            ApprovalResolution.from_payload(value),
            _identifier(value.get("claim_token"), "approval claim token"),
        )


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One durable approval row."""

    intent: ApprovalIntent
    state: ApprovalState
    resolution: ApprovalResolution | None
    checkpoint_id: str | None
    interrupt_id: str | None
    owner: str | None
    lease_deadline: float | None
    claim_token: str | None
    claim_consumed: bool


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
                resolution TEXT, checkpoint_id TEXT, interrupt_id TEXT, owner TEXT,
                lease_deadline REAL, claim_token TEXT, claim_consumed INTEGER NOT NULL DEFAULT 0,
                deadline REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS langgraph_approval_events (
                id INTEGER PRIMARY KEY, approval_ref TEXT NOT NULL, from_state TEXT,
                to_state TEXT NOT NULL, accepted INTEGER NOT NULL, occurred_at REAL NOT NULL);"""
            )
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(langgraph_approval_lifecycle)"
                ).fetchall()
            }
            for name, declaration in (
                ("interrupt_id", "TEXT"),
                ("claim_token", "TEXT"),
                ("claim_consumed", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE langgraph_approval_lifecycle "
                        f"ADD COLUMN {name} {declaration}"
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
            row["interrupt_id"],
            row["owner"],
            row["lease_deadline"],
            row["claim_token"],
            bool(row["claim_consumed"]),
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

    def begin_once(
        self, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> tuple[ApprovalRecord, bool]:
        """Persist one intent and report whether this delivery created it."""
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
        created = False
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO langgraph_approval_lifecycle "
                "(approval_ref, state, intent, deadline) VALUES (?, ?, ?, ?)",
                (ref, ApprovalState.AWAITING_CHECKPOINT.value, encoded, intent.deadline),
            )
            if cursor.rowcount:
                created = True
                self._event(connection, ref, None, ApprovalState.AWAITING_CHECKPOINT, True)
        current = self.get(ref)
        if current.intent.payload != intent.payload or current.intent.arguments != intent.arguments:
            with self._connect() as connection:
                self._event(
                    connection, ref, current.state, ApprovalState.AWAITING_CHECKPOINT, False
                )
            raise ToolGovernanceError("approval intent conflicts with its durable record")
        return current, created

    def begin(self, payload: Mapping[str, Any], arguments: Mapping[str, Any]) -> ApprovalRecord:
        """Persist one intent idempotently."""
        return self.begin_once(payload, arguments)[0]

    def _expire_locked(
        self, connection: sqlite3.Connection, ref: str, current: ApprovalRecord
    ) -> None:
        connection.execute(
            "UPDATE langgraph_approval_lifecycle SET state = ?, owner = NULL, "
            "lease_deadline = NULL, claim_token = NULL, claim_consumed = 0 "
            "WHERE approval_ref = ?",
            (ApprovalState.EXPIRED.value, ref),
        )
        self._event(connection, ref, current.state, ApprovalState.EXPIRED, True)

    def ready(self, ref: str, checkpoint_id: str, interrupt_id: str) -> ApprovalRecord:
        checkpoint_id = _identifier(checkpoint_id, "checkpoint id")
        interrupt_id = _identifier(interrupt_id, "interrupt id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                raise ToolGovernanceError("approval lifecycle record does not exist")
            current = self._record(row)
            if (
                current.checkpoint_id == checkpoint_id
                and current.interrupt_id == interrupt_id
                and current.state
                in (
                    ApprovalState.READY,
                    ApprovalState.DECIDED,
                    ApprovalState.RESUMING,
                    ApprovalState.RESOLVED,
                )
            ):
                return current
            if current.state is not ApprovalState.AWAITING_CHECKPOINT:
                self._event(connection, ref, current.state, ApprovalState.READY, False)
                connection.commit()
                raise ToolGovernanceError("approval checkpoint confirmation conflicts")
            if current.intent.deadline <= self._clock():
                self._expire_locked(connection, ref, current)
                connection.commit()
                raise ToolGovernanceError("approval deadline expired")
            connection.execute(
                "UPDATE langgraph_approval_lifecycle "
                "SET state = ?, checkpoint_id = ?, interrupt_id = ? WHERE approval_ref = ?",
                (ApprovalState.READY.value, checkpoint_id, interrupt_id, ref),
            )
            self._event(connection, ref, current.state, ApprovalState.READY, True)
        return self.get(ref)

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
            if (
                current.state.value not in _TERMINAL
                and not current.claim_consumed
                and current.intent.deadline <= self._clock()
            ):
                self._expire_locked(connection, ref, current)
                connection.commit()
                raise ToolGovernanceError("approval deadline expired")
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
                and (
                    current.claim_consumed
                    or (
                        current.lease_deadline is not None
                        and current.lease_deadline > now
                    )
                )
            ):
                return current, False
            if current.intent.deadline <= now:
                self._expire_locked(connection, ref, current)
                connection.commit()
                raise ToolGovernanceError("approval deadline expired")
            if current.state not in (ApprovalState.DECIDED, ApprovalState.RESUMING):
                self._event(connection, ref, current.state, ApprovalState.RESUMING, False)
                connection.commit()
                raise ToolGovernanceError("approval is not claimable")
            claim_token = uuid4().hex
            connection.execute(
                "UPDATE langgraph_approval_lifecycle "
                "SET state = ?, owner = ?, lease_deadline = ?, claim_token = ?, "
                "claim_consumed = 0 WHERE approval_ref = ?",
                (ApprovalState.RESUMING.value, owner, now + self._lease, claim_token, ref),
            )
            self._event(connection, ref, current.state, ApprovalState.RESUMING, True)
        return self.get(ref), True

    def claim(self, ref: str, *, owner: str) -> ApprovalRecord:
        """Claim resumable work, returning the current owner on duplicate delivery."""
        return self._claim(ref, owner=owner)[0]

    def finish(self, ref: str, claim_token: str) -> ApprovalRecord:
        """Resolve only the currently consumed delivery fence."""
        claim_token = _identifier(claim_token, "approval claim token")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                raise ToolGovernanceError("approval lifecycle record does not exist")
            current = self._record(row)
            if current.state is ApprovalState.RESOLVED and current.claim_token == claim_token:
                return current
            if (
                current.state is not ApprovalState.RESUMING
                or current.claim_token != claim_token
                or not current.claim_consumed
            ):
                self._event(connection, ref, current.state, ApprovalState.RESOLVED, False)
                connection.commit()
                raise ToolGovernanceError("approval completion fence is no longer current")
            connection.execute(
                "UPDATE langgraph_approval_lifecycle SET state = ?, lease_deadline = NULL "
                "WHERE approval_ref = ?",
                (ApprovalState.RESOLVED.value, ref),
            )
            self._event(connection, ref, current.state, ApprovalState.RESOLVED, True)
        return self.get(ref)

    def fail(self, ref: str, claim_token: str) -> ApprovalRecord:
        """Orphan the current failed delivery without replacing its exception."""
        claim_token = _identifier(claim_token, "approval claim token")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                raise ToolGovernanceError("approval lifecycle record does not exist")
            current = self._record(row)
            if current.state.value in _TERMINAL:
                return current
            if current.state is not ApprovalState.RESUMING or current.claim_token != claim_token:
                self._event(connection, ref, current.state, ApprovalState.ORPHANED, False)
                return current
            connection.execute(
                "UPDATE langgraph_approval_lifecycle SET state = ?, lease_deadline = NULL "
                "WHERE approval_ref = ?",
                (ApprovalState.ORPHANED.value, ref),
            )
            self._event(connection, ref, current.state, ApprovalState.ORPHANED, True)
        return self.get(ref)

    def terminal(self, ref: str, state: ApprovalState) -> ApprovalRecord:
        if state not in (ApprovalState.EXPIRED, ApprovalState.ORPHANED):
            raise ToolGovernanceError("invalid terminal approval state")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                raise ToolGovernanceError("approval lifecycle record does not exist")
            current = self._record(row)
            if current.state is state:
                return current
            if current.state.value in _TERMINAL:
                self._event(connection, ref, current.state, state, False)
                connection.commit()
                raise ToolGovernanceError("approval terminal state cannot be replaced")
            connection.execute(
                "UPDATE langgraph_approval_lifecycle SET state = ?, lease_deadline = NULL "
                "WHERE approval_ref = ?",
                (state.value, ref),
            )
            self._event(connection, ref, current.state, state, True)
        return self.get(ref)

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
        expired: list[ApprovalRecord] = []
        for record in self.pending(limit):
            ref = _identifier(record.intent.payload.get("approval_ref"), "approval ref")
            current = self._expire_due(ref)
            if current is not None:
                expired.append(current)
        return tuple(expired)

    def _expire_due(self, ref: str) -> ApprovalRecord | None:
        """Expire one still-unconsumed overdue row under the same write lock."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                return None
            current = self._record(row)
            if (
                current.state.value in _TERMINAL
                or current.claim_consumed
                or current.intent.deadline > self._clock()
            ):
                return None
            self._expire_locked(connection, ref, current)
        return self.get(ref)

    def consume(self, value: object) -> _ApprovalDelivery:
        delivery = _ApprovalDelivery.from_payload(value)
        ref = delivery.resolution.approval_ref
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                raise ToolGovernanceError("approval lifecycle record does not exist")
            current = self._record(row)
            if current.intent.deadline <= self._clock() and not current.claim_consumed:
                self._expire_locked(connection, ref, current)
                connection.commit()
                raise ToolGovernanceError("approval deadline expired")
            if (
                current.state is not ApprovalState.RESUMING
                or current.resolution != delivery.resolution
                or current.claim_token != delivery.claim_token
                or current.claim_consumed
            ):
                raise ToolGovernanceError("approval resume does not match the current claim fence")
            connection.execute(
                "UPDATE langgraph_approval_lifecycle SET claim_consumed = 1 "
                "WHERE approval_ref = ?",
                (ref,),
            )
        return delivery


def _langgraph_command(resume: Mapping[str, Any]) -> Any:
    from langgraph.types import Command

    return Command(resume=dict(resume))


class ApprovalCoordinator:
    """Confirm checkpoints and deliver decisions to their original threads."""

    def __init__(self, repository: SQLiteApprovalRepository) -> None:
        self._repository = repository

    @staticmethod
    def _config(
        config: Mapping[str, Any] | None,
        thread_id: str,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        if config is not None and not isinstance(config, Mapping):
            raise ApprovalRequiresThreadError("approval resume config must be a mapping")
        copied = dict(config or {})
        configurable = copied.get("configurable", {})
        if not isinstance(configurable, Mapping):
            raise ApprovalRequiresThreadError("approval resume configurable must be a mapping")
        positioned = {**configurable, "thread_id": thread_id}
        if checkpoint_id is not None:
            positioned["checkpoint_id"] = checkpoint_id
        else:
            positioned.pop("checkpoint_id", None)
        copied["configurable"] = positioned
        return copied

    @staticmethod
    def _require_durable_graph(graph: Any, durable_checkpointer: Any) -> None:
        try:
            from langgraph.checkpoint.base import BaseCheckpointSaver
            from langgraph.checkpoint.memory import InMemorySaver

            from zeroth.integrations.langgraph._wrapper import GovernedGraph

            actual = graph.checkpointer
        except Exception:
            raise ApprovalRequiresThreadError(
                "approval needs an inspectable durable LangGraph checkpointer"
            ) from None
        if (
            not isinstance(graph, GovernedGraph)
            or not isinstance(durable_checkpointer, BaseCheckpointSaver)
            or isinstance(durable_checkpointer, InMemorySaver)
            or actual is not durable_checkpointer
        ):
            raise ApprovalRequiresThreadError(
                "approval needs the governed graph's explicitly attested durable checkpointer"
            )

    def _observed(
        self,
        record: ApprovalRecord,
        graph: Any,
        config: Mapping[str, Any] | None,
    ) -> tuple[str | None, str | None]:
        thread = _identifier(record.intent.payload.get("thread_id"), "thread id")
        positioned = self._config(config, thread, record.checkpoint_id)
        try:
            snapshot = graph.get_state(positioned)
        except Exception:
            raise ApprovalRequiresThreadError(
                "approval checkpoint state is unavailable"
            ) from None
        config = getattr(snapshot, "config", None)
        configurable = config.get("configurable") if type(config) is dict else None
        checkpoint = configurable.get("checkpoint_id") if type(configurable) is dict else None
        valid = type(configurable) is dict and configurable.get("thread_id") == thread
        interrupts = getattr(snapshot, "interrupts", ())
        matches = (
            [
                item
                for item in interrupts
                if getattr(item, "value", None) == dict(record.intent.payload)
            ]
            if type(interrupts) is tuple
            else []
        )
        interrupt_id = getattr(matches[0], "id", None) if len(matches) == 1 else None
        return (
            checkpoint if valid and type(checkpoint) is str else None,
            interrupt_id if type(interrupt_id) is str and interrupt_id else None,
        )

    def confirm_checkpoint(
        self,
        ref: str,
        graph: Any,
        *,
        config: Mapping[str, Any] | None,
        durable_checkpointer: Any,
    ) -> ApprovalRecord:
        self._require_durable_graph(graph, durable_checkpointer)
        record = self._repository.get(ref)
        if (
            record.checkpoint_id is not None
            and record.interrupt_id is not None
            and record.state
            in (
                ApprovalState.READY,
                ApprovalState.DECIDED,
                ApprovalState.RESUMING,
                ApprovalState.RESOLVED,
            )
        ):
            return record
        checkpoint, interrupt_id = self._observed(record, graph, config)
        if checkpoint is None or interrupt_id is None:
            self._repository.terminal(ref, ApprovalState.ORPHANED)
            raise ToolGovernanceError("approval interrupt is not present in the original thread")
        try:
            return self._repository.ready(ref, checkpoint, interrupt_id)
        except ToolGovernanceError:
            current = self._repository.get(ref)
            if current.state.value not in _TERMINAL:
                self._repository.terminal(ref, ApprovalState.ORPHANED)
            raise

    def resume(
        self,
        ref: str,
        graph: Any,
        *,
        owner: str,
        config: Mapping[str, Any] | None,
        durable_checkpointer: Any,
    ) -> ApprovalRecord:
        record = self._repository.get(ref)
        if record.state.value in _TERMINAL:
            return record
        self._require_durable_graph(graph, durable_checkpointer)
        if record.state not in (ApprovalState.DECIDED, ApprovalState.RESUMING):
            raise ToolGovernanceError("approval is not ready to resume")
        claimed, acquired = self._repository._claim(ref, owner=owner)
        if not acquired:
            return claimed
        if (
            claimed.resolution is None
            or claimed.claim_token is None
            or claimed.interrupt_id is None
            or claimed.checkpoint_id is None
        ):
            if claimed.claim_token is not None:
                self._repository.fail(ref, claimed.claim_token)
            raise ToolGovernanceError("claimed approval has no decision")
        checkpoint, interrupt_id = self._observed(claimed, graph, config)
        if checkpoint != claimed.checkpoint_id or interrupt_id != claimed.interrupt_id:
            self._repository.fail(ref, claimed.claim_token)
            raise ToolGovernanceError("approval checkpoint changed before resume")
        thread = _identifier(claimed.intent.payload.get("thread_id"), "thread id")
        positioned = self._config(config, thread, claimed.checkpoint_id)
        delivery = _ApprovalDelivery(claimed.resolution, claimed.claim_token)
        try:
            graph.invoke(
                _langgraph_command({claimed.interrupt_id: delivery.to_payload()}), positioned
            )
        except BaseException:
            self._repository.fail(ref, claimed.claim_token)
            raise
        current = self._repository.get(ref)
        if current.state is not ApprovalState.RESOLVED:
            self._repository.fail(ref, claimed.claim_token)
            raise ToolGovernanceError("approval resume completed without tool resolution")
        return current
