"""Append-only SQLite fault observation store."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from zeroth.check.faults.models import FaultEvent, FaultEventKind, FaultName, FaultSpec


class FaultEvidenceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS check_fault_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                case_id TEXT NOT NULL,
                action_identity TEXT NOT NULL,
                fault_name TEXT NOT NULL,
                process_role TEXT NOT NULL,
                kind TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def append(
        self,
        spec: FaultSpec,
        kind: FaultEventKind,
        *,
        process_role: str,
        event_id: str | None = None,
    ) -> int:
        identifier = event_id or uuid.uuid4().hex
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO check_fault_events
                (event_id, case_id, action_identity, fault_name, process_role, kind)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    identifier,
                    spec.case_id,
                    spec.action_identity,
                    spec.name.value,
                    process_role,
                    kind.value,
                ),
            )
            return int(cursor.lastrowid)

    def events(self, spec: FaultSpec) -> tuple[tuple[int, FaultEvent], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sequence, event_id, case_id, action_identity, fault_name,
                process_role, kind FROM check_fault_events
                WHERE case_id = ? AND action_identity = ? AND fault_name = ?
                ORDER BY sequence""",
                (spec.case_id, spec.action_identity, spec.name.value),
            ).fetchall()
        return tuple(
            (
                int(row[0]),
                FaultEvent(
                    event_id=row[1],
                    case_id=row[2],
                    action_identity=row[3],
                    fault_name=FaultName(row[4]),
                    process_role=row[5],
                    kind=FaultEventKind(row[6]),
                ),
            )
            for row in rows
        )

    def marker_count(self, spec: FaultSpec) -> int:
        return sum(
            event.kind is FaultEventKind.EFFECT_MARKER_WRITTEN for _, event in self.events(spec)
        )
