from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path


def _module():
    return importlib.import_module("release.live_evaluation.sqlite_multiprocess_checkpoint")


def _operational_error(code: int, name: str) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("sensitive database details")
    error.sqlite_errorcode = code
    error.sqlite_errorname = name
    return error


def test_real_spawn_matrix_proves_supported_native_single_host_invariants(
    tmp_path: Path,
) -> None:
    module = _module()

    result = module.run_matrix(tmp_path / "matrix")

    assert result["all_passed"] is True
    assert result["process_start_method"] == "spawn"
    assert result["provider_calls"] == 0
    assert result["external_network_calls"] == 0
    assert result["scenarios"] == {
        "concurrent_unique_writes": {
            "status": "pass",
            "worker_count": 4,
            "expected_rows": 32,
            "actual_rows": 32,
            "duplicate_attempts": 96,
        },
        "bounded_lock_contention": {
            "status": "pass",
            "timed_out_fail_closed": True,
            "retry_after_release_committed": True,
            "actual_rows": 1,
        },
        "crash_before_commit": {
            "status": "pass",
            "crashed_exit_code": 17,
            "uncommitted_rows_after_restart": 0,
            "recovery_rows": 1,
        },
        "idempotent_sink_restart": {
            "status": "pass",
            "worker_count": 4,
            "effect_markers": 1,
            "matching_results": 4,
            "payload_conflict_rejected": True,
            "restart_lookup_matched": True,
            "worker_errors": [],
        },
        "integrity": {
            "status": "pass",
            "coordination_integrity": "ok",
            "sink_integrity": "ok",
            "coordination_journal_mode": "wal",
            "sink_journal_mode": "wal",
        },
    }


def test_mixed_sink_worker_observations_fail_the_matrix_closed() -> None:
    module = _module()
    passing = {
        "status": "pass",
        "payload_hash": "payload-hash",
        "receipt": "receipt",
    }
    observations = [{**passing, "worker_id": worker_id} for worker_id in range(3)] + [
        {
            "worker_id": 3,
            "status": "fail",
            "error_type": "OperationalError",
            "phase": "execute",
            "sqlite_errorcode": sqlite3.SQLITE_BUSY,
            "sqlite_errorname": "SQLITE_BUSY",
        }
    ]

    scenario = module._evaluate_sink_scenario(
        worker_count=4,
        exit_codes=[0, 0, 0, 0],
        observations=observations,
        effect_markers=1,
        payload_conflict_rejected=True,
        restart_lookup_matched=True,
    )

    assert scenario == {
        "status": "fail",
        "worker_count": 4,
        "effect_markers": 1,
        "matching_results": 3,
        "payload_conflict_rejected": True,
        "restart_lookup_matched": True,
        "worker_errors": [
            {
                "worker_id": 3,
                "error_type": "OperationalError",
                "phase": "execute",
                "sqlite_errorcode": sqlite3.SQLITE_BUSY,
                "sqlite_errorname": "SQLITE_BUSY",
            }
        ],
    }
    assert (
        module._all_scenarios_pass(
            {
                "idempotent_sink_restart": scenario,
                "integrity": {"status": "pass"},
            }
        )
        is False
    )


def test_sink_worker_reports_a_sanitized_execute_diagnostic(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _module()
    captured: list[dict[str, object]] = []

    class FailingSink:
        def __init__(self, _root: Path) -> None:
            pass

        def execute(self, *_args, **_kwargs):
            raise _operational_error(sqlite3.SQLITE_IOERR, "SQLITE_IOERR")

    class ImmediateStart:
        def wait(self) -> None:
            pass

    class CapturingQueue:
        def put(self, value: dict[str, object]) -> None:
            captured.append(value)

    monkeypatch.setattr(module, "EvaluationActionSink", FailingSink)

    module._sink_worker_process(
        str(tmp_path),
        3,
        ImmediateStart(),
        CapturingQueue(),
    )

    assert captured == [
        {
            "worker_id": 3,
            "status": "fail",
            "error_type": "OperationalError",
            "phase": "execute",
            "sqlite_errorcode": sqlite3.SQLITE_IOERR,
            "sqlite_errorname": "SQLITE_IOERR",
        }
    ]


def test_sink_worker_reports_a_sanitized_initialize_diagnostic(
    monkeypatch,
    tmp_path: Path,
) -> None:
    module = _module()
    captured: list[dict[str, object]] = []

    class FailingSink:
        def __init__(self, _root: Path) -> None:
            raise _operational_error(sqlite3.SQLITE_BUSY, "SQLITE_BUSY")

    class ImmediateStart:
        def wait(self) -> None:
            pass

    class CapturingQueue:
        def put(self, value: dict[str, object]) -> None:
            captured.append(value)

    monkeypatch.setattr(module, "EvaluationActionSink", FailingSink)

    module._sink_worker_process(
        str(tmp_path),
        2,
        ImmediateStart(),
        CapturingQueue(),
    )

    assert captured == [
        {
            "worker_id": 2,
            "status": "fail",
            "error_type": "OperationalError",
            "phase": "initialize",
            "sqlite_errorcode": sqlite3.SQLITE_BUSY,
            "sqlite_errorname": "SQLITE_BUSY",
        }
    ]


def test_checkpoint_seals_supported_scope_and_keeps_unsupported_topologies_blocked(
    tmp_path: Path,
) -> None:
    module = _module()
    root = tmp_path / "checkpoint"

    module.build_checkpoint(root=root, repository=Path(__file__).parents[2])

    acceptance = json.loads((root / "acceptance.json").read_text())
    by_id = {item["criterion_id"]: item for item in acceptance["criteria"]}
    assert by_id["SQLITE-NATIVE-SINGLE-HOST-MULTIPROCESS"]["status"] == "pass"
    assert by_id["SQLITE-DOCKER-BIND-MULTIPROCESS"]["status"] == "blocked"
    assert by_id["SQLITE-CROSS-HOST-COORDINATION"]["status"] == "blocked"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["live_service_mutated"] is False
    assert manifest["d012_touched"] is False
    assert manifest["provider_calls_performed"] == 0
    assert manifest["external_network_calls"] == 0
    assert manifest["scope"] == "native_single_host_filesystem"
    module.EvidenceStore(root).scan_recursive()
    assert (root / "database-snapshots" / "coordination.sqlite3").is_file()
    assert (root / "database-snapshots" / "action-sink.sqlite3").is_file()
    assert (root / "SHA256SUMS").is_file()


def test_nonpassing_matrix_cannot_become_a_supported_sqlite_pass(
    tmp_path: Path,
) -> None:
    module = _module()
    root = tmp_path / "checkpoint"

    failed = {
        "all_passed": False,
        "process_start_method": "spawn",
        "provider_calls": 0,
        "external_network_calls": 0,
        "scenarios": {"bounded_lock_contention": {"status": "fail"}},
        "database_paths": {},
    }
    module.build_checkpoint(
        root=root,
        repository=Path(__file__).parents[2],
        matrix_runner=lambda _root: failed,
    )

    acceptance = json.loads((root / "acceptance.json").read_text())
    by_id = {item["criterion_id"]: item for item in acceptance["criteria"]}
    assert by_id["SQLITE-NATIVE-SINGLE-HOST-MULTIPROCESS"]["status"] == "fail"
    assert "bounded_lock_contention" in by_id["SQLITE-NATIVE-SINGLE-HOST-MULTIPROCESS"]["note"]
