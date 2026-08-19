"""Durable execution claims for side-effecting LangGraph tool calls."""

from __future__ import annotations

import hashlib
import json
import math
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
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
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
                **identity,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ToolGovernanceError("persisted action execution record is invalid") from error

    def begin_once(
        self, action: ToolAction, context: ToolGovernanceContext
    ) -> tuple[ActionExecutionRecord, bool]:
        """Claim one action; only a new or explicitly failed attempt may execute."""
        identity = _identity(action, context)
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
        key = _action_key(identity)
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM langgraph_action_executions WHERE action_key = ?", (key,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO langgraph_action_executions "
                    "VALUES (?, ?, ?, NULL, 0, NULL, 1, ?, ?)",
                    (key, encoded, ActionExecutionState.IN_FLIGHT.value, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM langgraph_action_executions WHERE action_key = ?", (key,)
                ).fetchone()
                assert row is not None
                return self._record(row), True
            current = self._record(row)
            if row["identity_json"] != encoded:
                raise ToolGovernanceError("action execution identity collision")
            if current.state is ActionExecutionState.FAILED:
                connection.execute(
                    "UPDATE langgraph_action_executions SET state = ?, error_type = NULL, "
                    "attempts = attempts + 1, updated_at = ? WHERE action_key = ?",
                    (ActionExecutionState.IN_FLIGHT.value, now, key),
                )
                row = connection.execute(
                    "SELECT * FROM langgraph_action_executions WHERE action_key = ?", (key,)
                ).fetchone()
                assert row is not None
                return self._record(row), True
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
            return current, False

    def complete(self, action_key: str, result: Any) -> ActionExecutionRecord:
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
                "result_available = 1, updated_at = ? WHERE action_key = ? "
                "AND state IN (?, ?)",
                (
                    ActionExecutionState.COMPLETED.value,
                    result_json,
                    self._now(),
                    action_key,
                    ActionExecutionState.IN_FLIGHT.value,
                    ActionExecutionState.AMBIGUOUS.value,
                ),
            ).rowcount
            if changed != 1:
                raise ToolGovernanceError("action execution claim cannot be completed")
            row = connection.execute(
                "SELECT * FROM langgraph_action_executions WHERE action_key = ?", (action_key,)
            ).fetchone()
            assert row is not None
            return self._record(row)

    def fail(self, action_key: str, error: BaseException) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE langgraph_action_executions SET state = ?, error_type = ?, "
                "updated_at = ? WHERE action_key = ? AND state IN (?, ?)",
                (
                    ActionExecutionState.FAILED.value,
                    type(error).__name__,
                    self._now(),
                    action_key,
                    ActionExecutionState.IN_FLIGHT.value,
                    ActionExecutionState.AMBIGUOUS.value,
                ),
            )

    def replay_or_raise(self, record: ActionExecutionRecord) -> Any:
        if record.state is ActionExecutionState.COMPLETED and record.result_available:
            return record.result
        if record.state in (ActionExecutionState.IN_FLIGHT, ActionExecutionState.AMBIGUOUS):
            raise DuplicateToolExecutionError(
                f"tool action {record.action_key} is already in flight or ambiguous"
            )
        raise ToolGovernanceError("action execution cannot be replayed")

    def records(self) -> tuple[ActionExecutionRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM langgraph_action_executions ORDER BY created_at, action_key"
            ).fetchall()
        return tuple(self._record(row) for row in rows)


__all__ = [
    "ActionExecutionRecord",
    "ActionExecutionState",
    "SQLiteActionExecutionRepository",
    "result_fingerprint",
]
