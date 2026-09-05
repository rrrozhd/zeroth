"""Durable local-only action target for governed live-evaluation workflows."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

ActionSinkFault = Literal["unavailable", "timeout_after_commit"]


class ActionSinkUnavailableError(ConnectionError):
    """The controlled unavailable fault prevented the action from starting."""


class ActionPayloadConflictError(ValueError):
    """An operation key was reused with a different semantic payload."""


@dataclass(frozen=True)
class ActionReceipt:
    operation_key: str
    payload_hash: str
    receipt: str
    created_at: str
    duplicate: bool = False


class EvaluationActionSink:
    """SQLite action sink that can only create synthetic local markers."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "actions.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            # Concurrent first-time WAL conversions can return SQLITE_BUSY
            # without invoking SQLite's busy handler. Retry only setup, within
            # the existing connection budget; never replay an action write.
            deadline = time.monotonic() + 30
            while True:
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                connection.execute(f"PRAGMA busy_timeout={remaining_ms}")
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    remaining = deadline - time.monotonic()
                    if getattr(error, "sqlite_errorcode", 0) & 255 != sqlite3.SQLITE_BUSY:
                        raise
                    if remaining <= 0:
                        raise
                    time.sleep(min(0.005, remaining))
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=FULL")
        except BaseException:
            connection.close()
            raise
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS action_markers (
                    operation_key TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    receipt TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _canonical_payload(payload: Mapping[str, object]) -> tuple[str, str]:
        encoded = json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return encoded, hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _from_row(row: sqlite3.Row, *, duplicate: bool = False) -> ActionReceipt:
        return ActionReceipt(
            operation_key=str(row["operation_key"]),
            payload_hash=str(row["payload_hash"]),
            receipt=str(row["receipt"]),
            created_at=str(row["created_at"]),
            duplicate=duplicate,
        )

    def execute(
        self,
        operation_key: str,
        payload: Mapping[str, object],
        *,
        fault: ActionSinkFault | None = None,
    ) -> ActionReceipt:
        if fault == "unavailable":
            raise ActionSinkUnavailableError("evaluation action sink is unavailable")
        if not operation_key.strip():
            raise ValueError("operation_key must not be empty")

        payload_json, payload_hash = self._canonical_payload(payload)
        created_at = datetime.now(UTC).isoformat()
        receipt = f"local-evaluation:{operation_key}:{payload_hash[:16]}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM action_markers WHERE operation_key = ?", (operation_key,)
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != payload_hash:
                    raise ActionPayloadConflictError(
                        f"operation {operation_key!r} already has a different payload hash"
                    )
                return self._from_row(row, duplicate=True)
            connection.execute(
                """
                INSERT INTO action_markers (
                    operation_key, payload_hash, payload_json, receipt, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (operation_key, payload_hash, payload_json, receipt, created_at),
            )
        result = ActionReceipt(
            operation_key=operation_key,
            payload_hash=payload_hash,
            receipt=receipt,
            created_at=created_at,
        )
        if fault == "timeout_after_commit":
            raise TimeoutError("controlled timeout after commit")
        return result

    def lookup(self, operation_key: str) -> ActionReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_markers WHERE operation_key = ?", (operation_key,)
            ).fetchone()
        return None if row is None else self._from_row(row)

    def marker_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM action_markers").fetchone()
        assert row is not None
        return int(row["count"])
