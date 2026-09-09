from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from release.live_evaluation import action_sink
from release.live_evaluation.action_sink import (
    ActionPayloadConflictError,
    ActionSinkUnavailableError,
    EvaluationActionSink,
)


class _JournalModeConnection:
    def __init__(self, failures: list[sqlite3.OperationalError]) -> None:
        self.failures = failures
        self.attempts = 0

    def execute(self, statement: str) -> None:
        assert statement == "PRAGMA journal_mode=WAL"
        self.attempts += 1
        if self.failures:
            raise self.failures.pop(0)


def _sqlite_error(code: int, name: str) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(name)
    error.sqlite_errorcode = code
    error.sqlite_errorname = name
    return error


def test_wal_mode_retries_sqlite_busy_then_succeeds() -> None:
    connection = _JournalModeConnection([_sqlite_error(sqlite3.SQLITE_BUSY, "SQLITE_BUSY")])
    sleeps: list[float] = []

    action_sink._set_wal_mode(connection, sleep=sleeps.append)

    assert connection.attempts == 2
    assert sleeps == [0.01]


def test_wal_mode_propagates_non_busy_operational_error_immediately() -> None:
    error = _sqlite_error(sqlite3.SQLITE_IOERR, "SQLITE_IOERR")
    connection = _JournalModeConnection([error])

    with pytest.raises(sqlite3.OperationalError) as raised:
        action_sink._set_wal_mode(connection, sleep=pytest.fail)

    assert raised.value is error
    assert connection.attempts == 1


def test_wal_mode_propagates_busy_after_the_explicit_bound() -> None:
    failures = [
        _sqlite_error(sqlite3.SQLITE_BUSY, "SQLITE_BUSY")
        for _ in range(action_sink._WAL_BUSY_ATTEMPTS)
    ]
    final_error = failures[-1]
    connection = _JournalModeConnection(failures)
    sleeps: list[float] = []

    with pytest.raises(sqlite3.OperationalError) as raised:
        action_sink._set_wal_mode(connection, sleep=sleeps.append)

    assert raised.value is final_error
    assert connection.attempts == action_sink._WAL_BUSY_ATTEMPTS
    assert sleeps == [0.01, 0.02]


def test_action_sink_deduplicates_by_operation_and_payload_hash(tmp_path: Path) -> None:
    sink = EvaluationActionSink(tmp_path)

    first = sink.execute("op-1", {"ticket": "synthetic-1", "status": "remediated"})
    duplicate = sink.execute("op-1", {"status": "remediated", "ticket": "synthetic-1"})

    assert not first.duplicate
    assert duplicate.duplicate
    assert duplicate.receipt == first.receipt
    assert duplicate.payload_hash == first.payload_hash
    assert sink.marker_count() == 1


def test_action_sink_rejects_operation_reuse_with_different_payload(tmp_path: Path) -> None:
    sink = EvaluationActionSink(tmp_path)
    sink.execute("op-1", {"status": "one"})

    with pytest.raises(ActionPayloadConflictError):
        sink.execute("op-1", {"status": "two"})

    assert sink.marker_count() == 1


def test_timeout_after_commit_is_ambiguous_but_lookup_is_authoritative(tmp_path: Path) -> None:
    sink = EvaluationActionSink(tmp_path)

    with pytest.raises(TimeoutError, match="after commit"):
        sink.execute("op-timeout", {"status": "done"}, fault="timeout_after_commit")

    outcome = sink.lookup("op-timeout")
    assert outcome is not None
    assert outcome.operation_key == "op-timeout"
    assert sink.marker_count() == 1


def test_unavailable_fault_does_not_write_and_restart_preserves_receipt(tmp_path: Path) -> None:
    sink = EvaluationActionSink(tmp_path)
    with pytest.raises(ActionSinkUnavailableError):
        sink.execute("op-down", {"status": "never"}, fault="unavailable")
    assert sink.lookup("op-down") is None

    original = sink.execute("op-restart", {"status": "durable"})
    restarted = EvaluationActionSink(tmp_path)
    replay = restarted.execute("op-restart", {"status": "durable"})

    assert replay.duplicate
    assert replay.receipt == original.receipt
    assert restarted.marker_count() == 1
