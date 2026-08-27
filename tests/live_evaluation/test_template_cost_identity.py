from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from release.live_evaluation.template_cost_identity import (
    PersistedCostIdentityError,
    PersistedCostIdentityReader,
)


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "econ.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE cost_reservations (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT, run_id TEXT,
            status TEXT, cost_event_id TEXT, provider_request_id TEXT,
            cleanup_status TEXT)"""
        )
        db.execute(
            """CREATE TABLE execution_events (
            tenant_id TEXT, campaign_id TEXT, operation_id TEXT,
            execution_id TEXT, provider_request_id TEXT, cleanup_status TEXT,
            model_version TEXT, metadata TEXT)"""
        )
        db.execute(
            """INSERT INTO cost_reservations VALUES
            ('evaluation-studio-v1', 'evaluation-studio-v1', 'operation-1',
             'run-1', 'committed', 'cost-1', 'request-1', 'complete')"""
        )
        db.execute(
            """INSERT INTO execution_events VALUES
            ('evaluation-studio-v1', 'evaluation-studio-v1', 'operation-1',
             'cost-1', 'request-1', 'complete', 'openai/gpt-4o-mini', ?)""",
            (json.dumps({"run_id": "wrong-run", "provider_request_id": "wrong-request"}),),
        )
        # A hostile raw audit metadata value must never participate in the join.
        db.execute("CREATE TABLE node_audits (execution_metadata TEXT)")
        db.execute(
            "INSERT INTO node_audits VALUES (?)",
            (json.dumps({"provider_request_id": "wrong-audit-request"}),),
        )
    return path


def _reader(path: Path) -> PersistedCostIdentityReader:
    return PersistedCostIdentityReader(
        database=path,
        tenant_id="evaluation-studio-v1",
        campaign_id="evaluation-studio-v1",
        expected_provider="openai",
    )


def test_reads_exact_identity_from_first_class_persisted_planes_only(tmp_path: Path) -> None:
    database = _database(tmp_path)
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    identity = _reader(database)("cost-1", "run-1")

    assert identity == {
        "cost_event_id": "cost-1",
        "run_id": "run-1",
        "provider": "openai",
        "provider_request_id": "request-1",
    }
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("DELETE FROM cost_reservations", "reservation identity"),
        (
            "INSERT INTO cost_reservations SELECT * FROM cost_reservations",
            "reservation identity",
        ),
        (
            "UPDATE cost_reservations SET status='released'",
            "committed reservation",
        ),
        (
            "UPDATE cost_reservations SET provider_request_id=NULL",
            "provider request identity",
        ),
        (
            "UPDATE execution_events SET provider_request_id='different'",
            "execution identity",
        ),
        (
            "UPDATE execution_events SET model_version='anthropic/claude'",
            "provider identity",
        ),
    ],
)
def test_reader_fails_closed_on_missing_duplicate_or_drifting_identity(
    tmp_path: Path, mutation: str, message: str
) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute(mutation)

    with pytest.raises(PersistedCostIdentityError, match=message):
        _reader(database)("cost-1", "run-1")


def test_reader_rejects_wrong_requested_run_before_returning_identity(tmp_path: Path) -> None:
    database = _database(tmp_path)

    with pytest.raises(PersistedCostIdentityError, match="reservation identity"):
        _reader(database)("cost-1", "other-run")


def test_reader_fails_closed_when_authoritative_schema_is_unavailable(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite3"
    sqlite3.connect(database).close()

    with pytest.raises(PersistedCostIdentityError, match="schema"):
        _reader(database)("cost-1", "run-1")
