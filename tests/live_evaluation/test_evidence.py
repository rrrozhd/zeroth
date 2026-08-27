from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from release.live_evaluation.evidence import (
    AcceptanceCriterion,
    CorrelationIds,
    EvidenceStore,
    UnsafeEvidenceError,
)
from release.live_evaluation.runner import CommandSpec, execute_commands


def test_manifest_is_immutable_and_acceptance_uses_only_gate_states(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)

    manifest_path = store.write_manifest({"revision": "abc123", "diff_hash": "sha256:1"})
    acceptance_path = store.write_acceptance(
        [
            AcceptanceCriterion(
                criterion_id="control.audit-signed",
                status="pass",
                evidence=("events.ndjson#event-1",),
            )
        ]
    )

    assert json.loads(manifest_path.read_text())["revision"] == "abc123"
    assert json.loads(acceptance_path.read_text())["criteria"][0]["status"] == "pass"
    with pytest.raises(FileExistsError):
        store.write_manifest({"revision": "different"})


@pytest.mark.parametrize(
    "unsafe",
    [
        {"Authorization": "Bearer harmless-looking-value"},
        {"provider_key": "value"},
        {"message": "sk-proj-abcdefghijklmnopqrstuvwxyz123456"},
        {"message": "service_key=abcdefghijklmnopqrstuvwxyz123456"},
    ],
)
def test_evidence_rejects_headers_key_fields_and_secret_shaped_values(
    tmp_path: Path, unsafe: dict[str, str]
) -> None:
    store = EvidenceStore(tmp_path)

    with pytest.raises(UnsafeEvidenceError):
        store.append_event("event-unsafe", unsafe)

    assert not (tmp_path / "events.ndjson").exists()


def test_token_usage_is_economics_metadata_but_credential_token_fields_are_rejected(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)

    store.append_event("usage", {"token_usage": {"input": 3, "output": 2}})

    with pytest.raises(UnsafeEvidenceError):
        store.append_event("unsafe", {"access_token": "credential-value"})


def test_sqlite_scan_allows_authorization_audit_namespace_but_rejects_header_json(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    snapshot_dir = tmp_path / "database-snapshots"
    snapshot_dir.mkdir()
    snapshot = snapshot_dir / "zeroth-pretest.sqlite3"
    connection = sqlite3.connect(snapshot)
    connection.execute("create table node_audits (audit_id text, record_json text)")
    connection.execute(
        "insert into node_audits values (?, ?)",
        ("service.authorization:event-1", '{"audit_id":"service.authorization:event-1"}'),
    )
    connection.commit()
    connection.close()

    store.scan_recursive()

    connection = sqlite3.connect(snapshot)
    connection.execute(
        "insert into node_audits values (?, ?)",
        ("unsafe", '{"Authorization":"Bearer abcdefghijklmnopqrstuvwxyz"}'),
    )
    connection.commit()
    connection.close()

    with pytest.raises(UnsafeEvidenceError):
        store.scan_recursive()


def test_sqlite_scan_rejects_secret_named_columns_even_when_value_is_not_secret_shaped(
    tmp_path: Path,
) -> None:
    """Schema names are evidence too; random URL-safe secrets have no reliable prefix."""
    store = EvidenceStore(tmp_path)
    snapshot_dir = tmp_path / "database-snapshots"
    snapshot_dir.mkdir()
    snapshot = snapshot_dir / "webhooks.sqlite3"
    connection = sqlite3.connect(snapshot)
    connection.execute("create table webhook_subscriptions (subscription_id text, secret text)")
    connection.execute(
        "insert into webhook_subscriptions values (?, ?)",
        ("sub-1", "ordinary-looking-random-value"),
    )
    connection.commit()
    connection.close()

    with pytest.raises(
        UnsafeEvidenceError,
        match=r"forbidden evidence field.*webhook_subscriptions\.secret",
    ):
        store.scan_recursive()


def test_sqlite_scan_allows_authorization_audit_identity_columns(tmp_path: Path) -> None:
    """Authorization correlation IDs are audit metadata, not credential material."""
    store = EvidenceStore(tmp_path)
    snapshot_dir = tmp_path / "database-snapshots"
    snapshot_dir.mkdir()
    snapshot = snapshot_dir / "zeroth-pretest.sqlite3"
    connection = sqlite3.connect(snapshot)
    connection.execute(
        "create table retention_cleanup_state "
        "(run_id text, authorization_log_id text, authorization_event_id text)"
    )
    connection.execute(
        "insert into retention_cleanup_state values (?, ?, ?)",
        ("run-1", "log-1", "event-1"),
    )
    connection.commit()
    connection.close()

    store.scan_recursive()


def test_json_evidence_allows_authorization_audit_identity_fields(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)

    store.append_event(
        "retention.cleanup",
        {
            "authorization_log_id": "log-1",
            "authorization_event_id": "event-1",
        },
    )


def test_json_evidence_allows_authorization_presence_boolean_without_header_value(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)

    store.append_event(
        "credential.rejected",
        {
            "authorization_present": True,
            "authorization_value_retained": False,
        },
    )

    assert store.read_events()[0]["data"] == {
        "authorization_present": True,
        "authorization_value_retained": False,
    }
    with pytest.raises(UnsafeEvidenceError):
        store.append_event("unsafe", {"authorization_present": "random-looking-value"})
    with pytest.raises(UnsafeEvidenceError):
        store.append_event("unsafe", {"authorization_value_retained": True})


def test_event_append_and_command_capture_are_durable_and_safe(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.append_event(
        "provider.completed",
        {
            "estimated_cost_usd": "0.000002",
        },
        correlation=CorrelationIds(operation_id="op-1", provider_request_id="req-1"),
        event_id="event-1",
        timestamp="2026-08-22T12:00:00Z",
    )
    report = execute_commands(
        [
            CommandSpec(
                name="probe",
                argv=(
                    sys.executable,
                    "-c",
                    "import sys; print('visible'); print('warning', file=sys.stderr)",
                ),
            )
        ],
        artifact_root=tmp_path,
        evidence_store=store,
    )

    event_lines = (tmp_path / "events.ndjson").read_text().splitlines()
    command_record = json.loads((tmp_path / "commands" / "0001-probe.json").read_text())
    assert len(event_lines) == 2
    assert json.loads(event_lines[0])["event_id"] == "event-1"
    assert json.loads(event_lines[1])["type"] == "command.completed"
    assert command_record["exit_code"] == 0
    assert command_record["stdout"] == "visible\n"
    assert command_record["stderr"] == "warning\n"
    assert report.results[0].evidence_path == Path("commands/0001-probe.json")


def test_correlation_allows_many_operations_in_one_run_but_not_one_operation_in_many_runs(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    store.append_event(
        "campaign.audit.observed",
        {"node_id": "retrieve", "status": "completed"},
        correlation=CorrelationIds(
            run_id="run-1",
            operation_id="embedding-op",
            audit_event_id="embedding-audit",
            cost_event_id="embedding-cost",
            provider_request_id="embedding-request",
        ),
    )
    store.append_event(
        "campaign.audit.observed",
        {"node_id": "research", "status": "completed"},
        correlation=CorrelationIds(
            run_id="run-1",
            operation_id="chat-op",
            audit_event_id="chat-audit",
            cost_event_id="chat-cost",
            provider_request_id="chat-request",
        ),
    )

    with pytest.raises(ValueError, match="conflicting run_id"):
        store.append_event(
            "campaign.audit.observed",
            {"node_id": "research", "status": "completed"},
            correlation=CorrelationIds(
                run_id="run-2",
                operation_id="chat-op",
                audit_event_id="other-audit",
            ),
        )


def test_checksum_manifest_covers_bundle_without_self_reference(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    store.write_manifest({"revision": "abc123"})
    store.append_event("campaign.started", {"campaign_id": "evaluation-studio-v1"})

    checksums_path = store.write_checksums()
    lines = checksums_path.read_text().splitlines()

    assert [line.split("  ", 1)[1] for line in lines] == [
        "events.ndjson",
        "manifest.json",
    ]
    expected = hashlib.sha256((tmp_path / "manifest.json").read_bytes()).hexdigest()
    assert f"{expected}  manifest.json" in lines
    assert all("SHA256SUMS" not in line for line in lines)


def test_sqlite_snapshot_is_consistent_and_report_is_immutable(tmp_path: Path) -> None:
    source = tmp_path / "live.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES ('durable')")
    store = EvidenceStore(tmp_path / "bundle")

    snapshot = store.snapshot_sqlite(source, name="service-pretest.sqlite3")
    report = store.write_report("# Evaluation report\n\nControl plane: pass.\n")

    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == ("durable",)
    assert report.read_text().startswith("# Evaluation report")
    with pytest.raises(FileExistsError):
        store.snapshot_sqlite(source, name="service-pretest.sqlite3")
    with pytest.raises(FileExistsError):
        store.write_report("replacement")


def test_wal_snapshot_scan_does_not_create_mutable_sidecars_in_bundle(
    tmp_path: Path,
) -> None:
    source = tmp_path / "wal-live.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES ('durable')")
    store = EvidenceStore(tmp_path / "bundle")

    snapshot = store.snapshot_sqlite(source, name="service-pretest.sqlite3")
    store.scan_recursive()

    assert snapshot.is_file()
    assert not Path(f"{snapshot}-wal").exists()
    assert not Path(f"{snapshot}-shm").exists()
