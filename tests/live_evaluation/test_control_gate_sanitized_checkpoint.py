from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


CONTROL_IDS = (
    "control.revision-frozen",
    "control.diff-hashed",
    "control.database-snapshots",
    "control.audit-signed",
    "control.tenant-budget-10",
    "control.run-budget-025",
    "control.budget-concurrency",
    "control.budget-rejection",
    "control.budget-commit-release",
    "control.budget-recovery",
    "control.chroma-pinned-loopback",
    "stop.no-ambiguous-auto-retry",
    "economics.campaign-and-run-caps",
    "stop.cost-cap-enforced",
)


def _source(root: Path) -> Path:
    source = root / "source"
    (source / "database-snapshots").mkdir(parents=True)
    (source / "commands").mkdir()
    snapshot = source / "database-snapshots" / "zeroth-pretest.sqlite3"
    with sqlite3.connect(snapshot) as connection:
        connection.execute("create table webhook_subscriptions (secret text)")
        connection.execute("insert into webhook_subscriptions values ('private-value')")
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "abc123",
                "dirty_tree_hash": "sha256:diff",
                "pretest_sqlite_snapshots": ["database-snapshots/zeroth-pretest.sqlite3"],
            }
        )
    )
    (source / "events.ndjson").write_text(
        '{"event_id":"event-1","timestamp":"2026-08-22T00:00:00Z",'
        '"type":"control.ready","data":{"result":"pass"}}\n'
    )
    command_payloads = {
        "0006-budget-concurrency.json": {"admitted_count": 2},
        "0007-budget-commit-release.json": {
            "committed_usd": "0.01",
            "released_usd": "0.19",
        },
        "0008-budget-rejection.json": {
            "run_overage_rejected": True,
            "tenant_overage_rejected": True,
        },
        "0009-budget-recovery.json": {
            "recovered": True,
            "terminal_state_reconciled": True,
        },
    }
    for name, stdout in command_payloads.items():
        (source / "commands" / name).write_text(
            json.dumps(
                {
                    "argv": ["zeroth-local-control-gate", name[5:-5]],
                    "exit_code": 0,
                    "name": name[5:-5],
                    "stderr": "",
                    "stdout": json.dumps(stdout),
                    "working_directory": "/workspace",
                }
            )
        )
    (source / "acceptance.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "criteria": [
                    {
                        "criterion_id": criterion_id,
                        "status": (
                            "blocked"
                            if criterion_id
                            in {"economics.campaign-and-run-caps", "stop.cost-cap-enforced"}
                            else "pass"
                        ),
                        "evidence": ["events.ndjson#event-1"],
                        "note": None,
                    }
                    for criterion_id in CONTROL_IDS
                ],
            }
        )
    )
    files = sorted(path for path in source.rglob("*") if path.is_file())
    (source / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
            f"{path.relative_to(source).as_posix()}\n"
            for path in files
        )
    )
    return source


def test_checkpoint_attests_secret_bearing_snapshots_without_copying_them(
    tmp_path: Path,
) -> None:
    from release.live_evaluation.control_gate_sanitized_checkpoint import build_checkpoint
    from release.live_evaluation.evidence import EvidenceStore

    source = _source(tmp_path)
    destination = tmp_path / "accepted"

    build_checkpoint(source_root=source, destination_root=destination)

    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert {row["criterion_id"] for row in acceptance["criteria"]} == set(CONTROL_IDS)
    assert {row["status"] for row in acceptance["criteria"]} == {"pass"}
    attestations = json.loads(
        (destination / "database-snapshots/closed-snapshot-attestations.json").read_text()
    )
    assert attestations["raw_snapshots_in_sealed_bundle"] is False
    assert attestations["snapshots"][0]["quick_check"] == "ok"
    assert not any(path.suffix == ".sqlite3" for path in destination.rglob("*"))
    EvidenceStore(destination).scan_recursive()
    assert (destination / "SHA256SUMS").is_file()


def test_checkpoint_rejects_tampered_source_before_attesting(tmp_path: Path) -> None:
    import pytest

    from release.live_evaluation.control_gate_sanitized_checkpoint import build_checkpoint

    source = _source(tmp_path)
    (source / "events.ndjson").write_text("tampered\n")

    with pytest.raises(RuntimeError, match="checksum"):
        build_checkpoint(source_root=source, destination_root=tmp_path / "accepted")
