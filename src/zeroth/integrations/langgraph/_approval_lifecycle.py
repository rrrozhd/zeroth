"""Durable, fail-closed approval state for governed LangGraph tool calls."""

from __future__ import annotations

import asyncio
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
_IDENTITY_FIELDS = (
    "tenant_id",
    "principal_id",
    "run_id",
    "thread_id",
    "tool_fingerprint",
    "tool_call_id",
    "argument_fingerprint",
)
_IDENTITY_EXPRESSIONS = tuple(
    f"json_quote(json_extract(intent, '$.payload.{field}'))" for field in _IDENTITY_FIELDS
)


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
        self._resume_path = f"{self._path}.resume.sqlite3"
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
                        f"ALTER TABLE langgraph_approval_lifecycle ADD COLUMN {name} {declaration}"
                    )
            self._deduplicate_active_identities(connection)
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS langgraph_approval_active_identity "
                "ON langgraph_approval_lifecycle "
                f"({', '.join(_IDENTITY_EXPRESSIONS)}) "
                "WHERE state NOT IN ('resolved', 'expired', 'orphaned')"
            )
        with sqlite3.connect(self._resume_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS langgraph_approval_resume_lock "
                "(singleton INTEGER PRIMARY KEY CHECK (singleton = 1))"
            )
            connection.execute("INSERT OR IGNORE INTO langgraph_approval_resume_lock VALUES (1)")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _acquire_resume_lock(self) -> sqlite3.Connection:
        """Hold one cross-process SQLite writer fence for a graph resume."""
        # ponytail: one writer lock serializes all threads; shard by thread only
        # if measured approval throughput makes the safer global fence material.
        connection = sqlite3.connect(
            self._resume_path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE langgraph_approval_resume_lock SET singleton = 1 WHERE singleton = 1"
            )
        except sqlite3.Error as error:
            connection.close()
            raise ToolGovernanceError("approval resume coordination is unavailable") from error
        return connection

    @staticmethod
    def _release_resume_lock(connection: sqlite3.Connection) -> None:
        try:
            connection.rollback()
        finally:
            connection.close()

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

    def _deduplicate_active_identities(self, connection: sqlite3.Connection) -> None:
        """Keep the oldest active intent when upgrading a pre-constraint database."""
        selected = ", ".join(
            f"{expression} AS identity_{index}"
            for index, expression in enumerate(_IDENTITY_EXPRESSIONS)
        )
        rows = connection.execute(
            "SELECT rowid, *, "
            f"{selected} FROM langgraph_approval_lifecycle "
            "WHERE state NOT IN (?, ?, ?) ORDER BY rowid",
            _TERMINAL,
        ).fetchall()
        seen: set[tuple[Any, ...]] = set()
        for row in rows:
            identity = tuple(row[f"identity_{index}"] for index in range(len(_IDENTITY_FIELDS)))
            if identity in seen:
                self._orphan_locked(connection, row["approval_ref"], self._record(row))
            else:
                seen.add(identity)

    @staticmethod
    def _identity_values(identity: Mapping[str, Any]) -> tuple[Any, ...]:
        if any(field not in identity for field in _IDENTITY_FIELDS):
            raise ToolGovernanceError("approval action identity is incomplete")
        return tuple(identity[field] for field in _IDENTITY_FIELDS)

    def _active_row(
        self, connection: sqlite3.Connection, identity: tuple[Any, ...]
    ) -> sqlite3.Row | None:
        clauses = " AND ".join(
            f"json_extract(intent, '$.payload.{field}') IS ?" for field in _IDENTITY_FIELDS
        )
        rows = connection.execute(
            "SELECT * FROM langgraph_approval_lifecycle "
            "WHERE state NOT IN (?, ?, ?) "
            f"AND {clauses} LIMIT 2",
            (*_TERMINAL, *identity),
        ).fetchall()
        if len(rows) > 1:
            raise ToolGovernanceError("approval replay identity is ambiguous")
        return None if not rows else rows[0]

    def get(self, approval_ref: str) -> ApprovalRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?",
                (approval_ref,),
            ).fetchone()
        if row is None:
            raise ToolGovernanceError("approval lifecycle record does not exist")
        return self._record(row)

    def replay_for(self, identity: Mapping[str, Any]) -> ApprovalRecord | None:
        """Return the one unresolved approval matching this replayed action."""
        identity = _mapping(identity, "approval action identity")
        values = self._identity_values(identity)
        try:
            with self._connect() as connection:
                row = self._active_row(connection, values)
        except sqlite3.Error as error:
            raise ToolGovernanceError("approval replay lookup failed") from error
        return None if row is None else self._record(row)

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
        identity = self._identity_values(payload)
        intent = ApprovalIntent(payload, arguments, self._clock() + self._ttl)
        encoded = _dump(
            {
                "version": intent.version,
                "payload": dict(intent.payload),
                "arguments": dict(intent.arguments),
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO langgraph_approval_lifecycle "
                "(approval_ref, state, intent, deadline) VALUES (?, ?, ?, ?)",
                (ref, ApprovalState.AWAITING_CHECKPOINT.value, encoded, intent.deadline),
            )
            created = bool(cursor.rowcount)
            if created:
                self._event(connection, ref, None, ApprovalState.AWAITING_CHECKPOINT, True)
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            same_ref = row is not None
            if row is None:
                row = self._active_row(connection, identity)
            if row is None:
                raise ToolGovernanceError("approval intent could not be persisted")
            current = self._record(row)
            if (same_ref and current.intent.payload != intent.payload) or (
                current.intent.arguments != intent.arguments
            ):
                self._event(
                    connection,
                    row["approval_ref"],
                    current.state,
                    ApprovalState.AWAITING_CHECKPOINT,
                    False,
                )
                connection.commit()
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

    def _orphan_locked(
        self, connection: sqlite3.Connection, ref: str, current: ApprovalRecord
    ) -> None:
        connection.execute(
            "UPDATE langgraph_approval_lifecycle SET state = ?, lease_deadline = NULL "
            "WHERE approval_ref = ?",
            (ApprovalState.ORPHANED.value, ref),
        )
        self._event(connection, ref, current.state, ApprovalState.ORPHANED, True)

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
            if current.state is ApprovalState.RESUMING and current.claim_consumed:
                if current.lease_deadline is not None and current.lease_deadline > now:
                    return current, False
                self._orphan_locked(connection, ref, current)
                row = connection.execute(
                    "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?",
                    (ref,),
                ).fetchone()
                assert row is not None
                return self._record(row), False
            if (
                current.state is ApprovalState.RESUMING
                and current.lease_deadline is not None
                and current.lease_deadline > now
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
        """Terminalize one overdue request or uncertain consumed delivery."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_approval_lifecycle WHERE approval_ref = ?", (ref,)
            ).fetchone()
            if row is None:
                return None
            current = self._record(row)
            now = self._clock()
            if current.state.value in _TERMINAL:
                return None
            if current.claim_consumed:
                if current.lease_deadline is not None and current.lease_deadline > now:
                    return None
                self._orphan_locked(connection, ref, current)
            elif current.intent.deadline <= now:
                self._expire_locked(connection, ref, current)
            else:
                return None
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
                "UPDATE langgraph_approval_lifecycle SET claim_consumed = 1 WHERE approval_ref = ?",
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

    @staticmethod
    def _snapshot_observation(
        record: ApprovalRecord, snapshot: Any
    ) -> tuple[str | None, str | None]:
        thread = _identifier(record.intent.payload.get("thread_id"), "thread id")
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

    def _observed(
        self,
        record: ApprovalRecord,
        graph: Any,
        config: Mapping[str, Any] | None,
    ) -> tuple[str | None, str | None]:
        thread = _identifier(record.intent.payload.get("thread_id"), "thread id")
        try:
            snapshot = graph.get_state(self._config(config, thread))
        except Exception:
            raise ApprovalRequiresThreadError("approval checkpoint state is unavailable") from None
        return self._snapshot_observation(record, snapshot)

    async def _aobserved(
        self,
        record: ApprovalRecord,
        graph: Any,
        config: Mapping[str, Any] | None,
    ) -> tuple[str | None, str | None]:
        thread = _identifier(record.intent.payload.get("thread_id"), "thread id")
        try:
            snapshot = await graph.aget_state(self._config(config, thread))
        except Exception:
            raise ApprovalRequiresThreadError("approval checkpoint state is unavailable") from None
        return self._snapshot_observation(record, snapshot)

    def _confirmed(
        self, ref: str, checkpoint: str | None, interrupt_id: str | None
    ) -> ApprovalRecord:
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
        return self._confirmed(ref, checkpoint, interrupt_id)

    async def aconfirm_checkpoint(
        self,
        ref: str,
        graph: Any,
        *,
        config: Mapping[str, Any] | None,
        durable_checkpointer: Any,
    ) -> ApprovalRecord:
        """Async checkpoint confirmation for async-only durable savers."""
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
        checkpoint, interrupt_id = await self._aobserved(record, graph, config)
        return self._confirmed(ref, checkpoint, interrupt_id)

    def _claimed(self, ref: str, owner: str) -> tuple[ApprovalRecord, _ApprovalDelivery | None]:
        claimed, acquired = self._repository._claim(ref, owner=owner)
        if not acquired:
            return claimed, None
        if (
            claimed.resolution is None
            or claimed.claim_token is None
            or claimed.interrupt_id is None
            or claimed.checkpoint_id is None
        ):
            if claimed.claim_token is not None:
                self._repository.fail(ref, claimed.claim_token)
            raise ToolGovernanceError("claimed approval has no decision")
        return claimed, _ApprovalDelivery(claimed.resolution, claimed.claim_token)

    def _current_resume_config(
        self,
        claimed: ApprovalRecord,
        checkpoint: str | None,
        interrupt_id: str | None,
        config: Mapping[str, Any] | None,
        graph: Any,
    ) -> dict[str, Any]:
        if checkpoint is None or interrupt_id != claimed.interrupt_id:
            assert claimed.claim_token is not None
            self._repository.fail(
                _identifier(claimed.intent.payload.get("approval_ref"), "approval ref"),
                claimed.claim_token,
            )
            raise ToolGovernanceError("approval interrupt changed before resume")
        thread = _identifier(claimed.intent.payload.get("thread_id"), "thread id")
        positioned = self._config(config, thread)
        from zeroth.integrations.langgraph._wrapper import (
            _RESUME_LATEST_CHECKPOINT,
            GovernedGraph,
        )

        if isinstance(graph, GovernedGraph):
            positioned["configurable"][_RESUME_LATEST_CHECKPOINT] = True
        return positioned

    def _completed(self, ref: str, claim_token: str) -> ApprovalRecord:
        current = self._repository.get(ref)
        if current.state is not ApprovalState.RESOLVED:
            self._repository.fail(ref, claim_token)
            raise ToolGovernanceError("approval resume completed without tool resolution")
        return current

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
        resume_lock = self._repository._acquire_resume_lock()
        try:
            claimed, delivery = self._claimed(ref, owner)
            if delivery is None:
                return claimed
            checkpoint, interrupt_id = self._observed(claimed, graph, config)
            positioned = self._current_resume_config(
                claimed, checkpoint, interrupt_id, config, graph
            )
            assert claimed.interrupt_id is not None and claimed.claim_token is not None
            try:
                graph.invoke(
                    _langgraph_command({claimed.interrupt_id: delivery.to_payload()}),
                    positioned,
                )
            except BaseException:
                self._repository.fail(ref, claimed.claim_token)
                raise
            return self._completed(ref, claimed.claim_token)
        finally:
            self._repository._release_resume_lock(resume_lock)

    async def aresume(
        self,
        ref: str,
        graph: Any,
        *,
        owner: str,
        config: Mapping[str, Any] | None,
        durable_checkpointer: Any,
    ) -> ApprovalRecord:
        """Async resume with the same durable fencing as :meth:`resume`."""
        record = self._repository.get(ref)
        if record.state.value in _TERMINAL:
            return record
        self._require_durable_graph(graph, durable_checkpointer)
        if record.state not in (ApprovalState.DECIDED, ApprovalState.RESUMING):
            raise ToolGovernanceError("approval is not ready to resume")
        resume_lock = await asyncio.to_thread(self._repository._acquire_resume_lock)
        try:
            claimed, delivery = self._claimed(ref, owner)
            if delivery is None:
                return claimed
            checkpoint, interrupt_id = await self._aobserved(claimed, graph, config)
            positioned = self._current_resume_config(
                claimed, checkpoint, interrupt_id, config, graph
            )
            assert claimed.interrupt_id is not None and claimed.claim_token is not None
            try:
                await graph.ainvoke(
                    _langgraph_command({claimed.interrupt_id: delivery.to_payload()}),
                    positioned,
                )
            except BaseException:
                self._repository.fail(ref, claimed.claim_token)
                raise
            return self._completed(ref, claimed.claim_token)
        finally:
            self._repository._release_resume_lock(resume_lock)
