"""Durable execution claims for side-effecting LangGraph tool calls."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from zeroth.integrations.langgraph._tool_errors import (
    DuplicateToolExecutionError,
    ToolGovernanceError,
)
from zeroth.integrations.langgraph._tool_normalize import argument_fingerprint
from zeroth.integrations.langgraph._tool_types import ToolAction, ToolGovernanceContext


class ActionExecutionState(StrEnum):
    """Durable outcomes for one logical tool execution."""

    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"
    UNREPLAYABLE = "unreplayable"


@dataclass(frozen=True, slots=True)
class ActionExecutionRecord:
    """One stable action identity and its latest durable outcome."""

    action_key: str
    state: ActionExecutionState
    tenant_id: str
    principal_id: str
    run_id: str
    thread_id: str | None
    tool_fingerprint: str
    tool_call_id: str
    argument_fingerprint: str
    result: Any = None
    result_available: bool = False
    attempts: int = 1
    claim_open: bool = False


@dataclass(frozen=True, slots=True)
class ActionExecutionClaim:
    """Opaque authority held only by the delivery allowed to execute."""

    record: ActionExecutionRecord
    claim_token: str | None
    may_execute: bool


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    """Operator-attributed evidence for resolving an ambiguous action."""

    action_key: str
    operator_ref: str
    reason_code: str
    prior_state: ActionExecutionState
    new_state: ActionExecutionState
    receipt_fingerprint: str | None
    reconciled_at: float


def _identity(action: ToolAction, context: ToolGovernanceContext) -> dict[str, Any]:
    if type(action) is not ToolAction or type(context) is not ToolGovernanceContext:
        raise ToolGovernanceError("an action execution claim needs normalized identity")
    if action.tool_call_id is None:
        raise ToolGovernanceError(
            "a side-effecting action execution claim needs a stable tool-call id"
        )
    return {
        "tenant_id": context.tenant_id,
        "principal_id": context.principal_id,
        "run_id": context.run_id,
        "thread_id": context.thread_id,
        "tool_fingerprint": action.identity.fingerprint,
        "tool_call_id": action.tool_call_id,
        "argument_fingerprint": argument_fingerprint(action.arguments),
    }


def _action_key(identity: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(identity), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return f"act_{hashlib.sha256(encoded).hexdigest()}"


def result_fingerprint(result: Any) -> str:
    """Return the content-free digest stored in approval execution evidence."""
    try:
        encoded = json.dumps(
            result, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    except (TypeError, ValueError) as error:
        raise ToolGovernanceError("tool result is not durably replayable JSON") from error
    return hashlib.sha256(encoded).hexdigest()


class SQLiteActionExecutionRepository:
    """Cross-process first-claim-wins fence for external LangGraph tool calls."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        if str(path) in ("", ":memory:"):
            raise ToolGovernanceError("action execution claims need durable storage")
        self._path = str(Path(path))
        self._clock = clock
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS langgraph_action_executions (
                action_key TEXT PRIMARY KEY,
                identity_json TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                result_available INTEGER NOT NULL DEFAULT 0,
                error_type TEXT,
                attempts INTEGER NOT NULL DEFAULT 1,
                claim_token_digest TEXT,
                claim_open INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
                )"""
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(langgraph_action_executions)"
                ).fetchall()
            }
            if "claim_token_digest" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_action_executions ADD COLUMN claim_token_digest TEXT"
                )
            if "claim_open" not in columns:
                connection.execute(
                    "ALTER TABLE langgraph_action_executions "
                    "ADD COLUMN claim_open INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS langgraph_action_reconciliations (
                reconciliation_id TEXT PRIMARY KEY,
                action_key TEXT NOT NULL,
                operator_ref TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                prior_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                receipt_fingerprint TEXT,
                reconciled_at REAL NOT NULL
                )"""
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        try:
            with sqlite3.connect(self._path, timeout=30) as connection:
                connection.row_factory = sqlite3.Row
                yield connection
        except sqlite3.Error as error:
            raise ToolGovernanceError(
                "action execution lifecycle storage is unavailable"
            ) from error

    def _now(self) -> float:
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ToolGovernanceError("action execution clock must return a finite number")
        return float(value)

    @staticmethod
    def _record(row: sqlite3.Row) -> ActionExecutionRecord:
        try:
            identity = json.loads(row["identity_json"])
            result_available = bool(row["result_available"])
            result = json.loads(row["result_json"]) if result_available else None
            return ActionExecutionRecord(
                action_key=row["action_key"],
                state=ActionExecutionState(row["state"]),
                result=result,
                result_available=result_available,
                attempts=int(row["attempts"]),
                claim_open=bool(row["claim_open"]),
                **identity,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ToolGovernanceError("persisted action execution record is invalid") from error

    @staticmethod
    def _claim_digest(claim_token: str) -> str:
        return hashlib.sha256(claim_token.encode()).hexdigest()

    def begin_once(
        self, action: ToolAction, context: ToolGovernanceContext
    ) -> ActionExecutionClaim:
        """Claim one action; only a new or explicitly failed attempt may execute."""
        identity = _identity(action, context)
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
        key = _action_key(identity)
        now = self._now()
        claim_token = secrets.token_urlsafe(32)
        claim_digest = self._claim_digest(claim_token)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_action_executions WHERE action_key = ?", (key,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO langgraph_action_executions "
                    "(action_key, identity_json, state, result_json, result_available, "
                    "error_type, attempts, claim_token_digest, claim_open, created_at, "
                    "updated_at) VALUES (?, ?, ?, NULL, 0, NULL, 1, ?, 1, ?, ?)",
                    (
                        key,
                        encoded,
                        ActionExecutionState.IN_FLIGHT.value,
                        claim_digest,
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM langgraph_action_executions WHERE action_key = ?", (key,)
                ).fetchone()
                assert row is not None
                return ActionExecutionClaim(self._record(row), claim_token, True)
            current = self._record(row)
            if row["identity_json"] != encoded:
                raise ToolGovernanceError("action execution identity collision")
            if current.state is ActionExecutionState.FAILED:
                connection.execute(
                    "UPDATE langgraph_action_executions SET state = ?, error_type = NULL, "
                    "attempts = attempts + 1, claim_token_digest = ?, claim_open = 1, "
                    "updated_at = ? WHERE action_key = ? AND state = ?",
                    (
                        ActionExecutionState.IN_FLIGHT.value,
                        claim_digest,
                        now,
                        key,
                        ActionExecutionState.FAILED.value,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM langgraph_action_executions WHERE action_key = ?", (key,)
                ).fetchone()
                assert row is not None
                return ActionExecutionClaim(self._record(row), claim_token, True)
            if current.state is ActionExecutionState.IN_FLIGHT:
                connection.execute(
                    "UPDATE langgraph_action_executions SET state = ?, updated_at = ? "
                    "WHERE action_key = ? AND state = ?",
                    (
                        ActionExecutionState.AMBIGUOUS.value,
                        now,
                        key,
                        ActionExecutionState.IN_FLIGHT.value,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM langgraph_action_executions WHERE action_key = ?", (key,)
                ).fetchone()
                assert row is not None
                current = self._record(row)
            return ActionExecutionClaim(current, None, False)

    def complete(self, claim: ActionExecutionClaim, result: Any) -> ActionExecutionRecord:
        """Persist a replayable result before the first worker reports success."""
        try:
            result_json = json.dumps(
                result, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as error:
            raise ToolGovernanceError("tool result is not durably replayable JSON") from error
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE langgraph_action_executions SET state = ?, result_json = ?, "
                "result_available = 1, claim_open = 0, updated_at = ? WHERE action_key = ? "
                "AND claim_open = 1 AND claim_token_digest = ? AND state IN (?, ?)",
                (
                    ActionExecutionState.COMPLETED.value,
                    result_json,
                    self._now(),
                    claim.record.action_key,
                    self._claim_digest(claim.claim_token or ""),
                    ActionExecutionState.IN_FLIGHT.value,
                    ActionExecutionState.AMBIGUOUS.value,
                ),
            ).rowcount
            if changed != 1:
                raise ToolGovernanceError("action execution claim cannot be completed")
            row = connection.execute(
                "SELECT * FROM langgraph_action_executions WHERE action_key = ?",
                (claim.record.action_key,),
            ).fetchone()
            assert row is not None
            return self._record(row)

    def fail_pre_effect(
        self, claim: ActionExecutionClaim, error: BaseException
    ) -> ActionExecutionRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE langgraph_action_executions SET state = ?, error_type = ?, "
                "claim_open = 0, updated_at = ? WHERE action_key = ? AND state IN (?, ?) "
                "AND claim_open = 1 AND claim_token_digest = ?",
                (
                    ActionExecutionState.FAILED.value,
                    type(error).__name__,
                    self._now(),
                    claim.record.action_key,
                    ActionExecutionState.IN_FLIGHT.value,
                    ActionExecutionState.AMBIGUOUS.value,
                    self._claim_digest(claim.claim_token or ""),
                ),
            ).rowcount
            if changed != 1:
                raise ToolGovernanceError("action execution claim cannot fail pre-effect")
            return self._load(connection, claim.record.action_key)

    def mark_ambiguous(
        self,
        claim: ActionExecutionClaim,
        error: BaseException,
        *,
        close_claim: bool,
    ) -> ActionExecutionRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE langgraph_action_executions SET state = ?, error_type = ?, "
                "claim_open = ?, updated_at = ? WHERE action_key = ? "
                "AND state IN (?, ?) AND claim_open = 1 AND claim_token_digest = ?",
                (
                    ActionExecutionState.AMBIGUOUS.value,
                    type(error).__name__,
                    0 if close_claim else 1,
                    self._now(),
                    claim.record.action_key,
                    ActionExecutionState.IN_FLIGHT.value,
                    ActionExecutionState.AMBIGUOUS.value,
                    self._claim_digest(claim.claim_token or ""),
                ),
            ).rowcount
            if changed != 1:
                raise ToolGovernanceError("action execution claim cannot become ambiguous")
            return self._load(connection, claim.record.action_key)

    def _load(self, connection: sqlite3.Connection, action_key: str) -> ActionExecutionRecord:
        row = connection.execute(
            "SELECT * FROM langgraph_action_executions WHERE action_key = ?", (action_key,)
        ).fetchone()
        if row is None:
            raise ToolGovernanceError("action execution record does not exist")
        return self._record(row)

    @staticmethod
    def _operator_ref(value: str) -> str:
        if type(value) is not str or not value.strip():
            raise ToolGovernanceError("reconciliation needs an operator reference")
        return value.strip()

    def reconcile_completed(
        self, action_key: str, result: Any, operator_ref: str
    ) -> ReconciliationRecord:
        operator = self._operator_ref(operator_ref)
        receipt = result_fingerprint(result)
        result_json = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return self._reconcile(
            action_key,
            operator,
            ActionExecutionState.COMPLETED,
            "reconciled_downstream_receipt",
            receipt,
            result_json,
        )

    def reconcile_no_effect(
        self, action_key: str, operator_ref: str
    ) -> ReconciliationRecord:
        return self._reconcile(
            action_key,
            self._operator_ref(operator_ref),
            ActionExecutionState.FAILED,
            "reconciled_verified_no_effect",
            None,
            None,
        )

    def _reconcile(
        self,
        action_key: str,
        operator_ref: str,
        new_state: ActionExecutionState,
        reason_code: str,
        receipt: str | None,
        result_json: str | None,
    ) -> ReconciliationRecord:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._load(connection, action_key)
            if current.state is not ActionExecutionState.AMBIGUOUS:
                raise ToolGovernanceError("only an ambiguous action may be reconciled")
            changed = connection.execute(
                "UPDATE langgraph_action_executions SET state = ?, result_json = ?, "
                "result_available = ?, claim_open = 0, updated_at = ? "
                "WHERE action_key = ? AND state = ?",
                (
                    new_state.value,
                    result_json,
                    1 if new_state is ActionExecutionState.COMPLETED else 0,
                    now,
                    action_key,
                    ActionExecutionState.AMBIGUOUS.value,
                ),
            ).rowcount
            if changed != 1:
                raise ToolGovernanceError("action reconciliation conflicted")
            reconciliation_id = f"rec_{secrets.token_hex(16)}"
            connection.execute(
                "INSERT INTO langgraph_action_reconciliations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reconciliation_id,
                    action_key,
                    operator_ref,
                    reason_code,
                    current.state.value,
                    new_state.value,
                    receipt,
                    now,
                ),
            )
        return ReconciliationRecord(
            action_key=action_key,
            operator_ref=operator_ref,
            reason_code=reason_code,
            prior_state=current.state,
            new_state=new_state,
            receipt_fingerprint=receipt,
            reconciled_at=now,
        )

    def mark_unreplayable(self, claim: ActionExecutionClaim, error: BaseException) -> None:
        """Retire a claim whose side effect ran but whose result cannot be persisted.

        The tool already executed, so the record must be terminal -- never left
        ``IN_FLIGHT``/``AMBIGUOUS``, which a redelivery would read as "still in
        flight" and raise :class:`DuplicateToolExecutionError` on forever. Unlike
        :meth:`fail`, this state is not re-armed by :meth:`begin_once`: the effect
        must not run a second time just because its first result was unstorable.

        Takes the claim, not a bare action key, and matches on the claim token the
        way :meth:`mark_ambiguous` does. UNREPLAYABLE is terminal and is never
        re-armed, so keying on the action alone would let a worker that has since
        been fenced retire a claim another worker legitimately holds -- turning
        the fencing this class exists to provide into a way to strand live work.
        The claim is closed on the way out: its holder is done either way.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE langgraph_action_executions SET state = ?, error_type = ?, "
                "claim_open = 0, updated_at = ? WHERE action_key = ? "
                "AND state IN (?, ?) AND claim_open = 1 AND claim_token_digest = ?",
                (
                    ActionExecutionState.UNREPLAYABLE.value,
                    type(error).__name__,
                    self._now(),
                    claim.record.action_key,
                    ActionExecutionState.IN_FLIGHT.value,
                    ActionExecutionState.AMBIGUOUS.value,
                    self._claim_digest(claim.claim_token or ""),
                ),
            ).rowcount
            if changed != 1:
                raise ToolGovernanceError(
                    "action execution claim cannot become unreplayable"
                )

    def replay_or_raise(self, record: ActionExecutionRecord) -> Any:
        if record.state is ActionExecutionState.COMPLETED and record.result_available:
            return record.result
        if record.state in (ActionExecutionState.IN_FLIGHT, ActionExecutionState.AMBIGUOUS):
            raise DuplicateToolExecutionError(
                f"tool action {record.action_key} is already in flight or ambiguous"
            )
        if record.state is ActionExecutionState.UNREPLAYABLE:
            raise ToolGovernanceError(
                f"tool action {record.action_key} already executed but its result "
                "is not durably replayable; do not reissue"
            )
        raise ToolGovernanceError("action execution cannot be replayed")

    def records(self) -> tuple[ActionExecutionRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM langgraph_action_executions ORDER BY created_at, action_key"
            ).fetchall()
        return tuple(self._record(row) for row in rows)

    def reconciliations(self, action_key: str | None = None) -> tuple[ReconciliationRecord, ...]:
        with self._connect() as connection:
            if action_key is None:
                rows = connection.execute(
                    "SELECT * FROM langgraph_action_reconciliations "
                    "ORDER BY reconciled_at, reconciliation_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM langgraph_action_reconciliations WHERE action_key = ? "
                    "ORDER BY reconciled_at, reconciliation_id",
                    (action_key,),
                ).fetchall()
        return tuple(
            ReconciliationRecord(
                action_key=row["action_key"],
                operator_ref=row["operator_ref"],
                reason_code=row["reason_code"],
                prior_state=ActionExecutionState(row["prior_state"]),
                new_state=ActionExecutionState(row["new_state"]),
                receipt_fingerprint=row["receipt_fingerprint"],
                reconciled_at=float(row["reconciled_at"]),
            )
            for row in rows
        )


__all__ = [
    "ActionExecutionClaim",
    "ActionExecutionRecord",
    "ActionExecutionState",
    "ReconciliationRecord",
    "SQLiteActionExecutionRepository",
    "result_fingerprint",
]
