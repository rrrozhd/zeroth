"""Prove the supported same-host SQLite persistence boundary with real processes.

The live campaign previously let two Docker containers write one host bind-mounted
database.  Docker Desktop's filesystem bridge is not a supported SQLite locking
domain, so this checkpoint deliberately does not repeat or bless that topology.
It exercises Zeroth's production SQLite wrapper and local action sink on one
native filesystem with spawned processes, bounded lock contention, abrupt process
exit, restart, and idempotent duplicate delivery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from release.live_evaluation.action_sink import (
    ActionPayloadConflictError,
    EvaluationActionSink,
)
from release.live_evaluation.control_plane import dirty_tree_hash
from release.live_evaluation.evidence import AcceptanceCriterion, EvidenceStore
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.platform.storage.database import CoordinationTimeoutError

_WORKER_COUNT = 4
_OPERATION_COUNT = 32
_SPAWN_TIMEOUT_SECONDS = 30.0

Matrix = dict[str, Any]
MatrixRunner = Callable[[Path], Matrix]


async def _initialize_coordination_database(path: str) -> None:
    database = AsyncSQLiteDatabase(path, coordination_timeout_seconds=10.0)
    async with database.transaction(write_lock=True) as connection:
        await connection.execute_script(
            """
            CREATE TABLE durable_operations (
                operation_id TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                worker_id INTEGER NOT NULL
            );
            CREATE TABLE lock_recovery (
                operation_id TEXT PRIMARY KEY,
                worker_id INTEGER NOT NULL
            );
            CREATE TABLE crash_recovery (
                operation_id TEXT PRIMARY KEY,
                worker_id INTEGER NOT NULL
            );
            """
        )


async def _concurrent_writer(path: str, worker_id: int) -> None:
    database = AsyncSQLiteDatabase(path, coordination_timeout_seconds=10.0)
    for index in range(_OPERATION_COUNT):
        async with database.transaction(write_lock=True) as connection:
            await connection.execute(
                "INSERT OR IGNORE INTO durable_operations "
                "(operation_id, payload_hash, worker_id) VALUES (?, ?, ?)",
                (f"operation-{index:03d}", f"payload-{index:03d}", worker_id),
            )


def _concurrent_writer_process(
    path: str,
    worker_id: int,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    start.wait()
    try:
        asyncio.run(_concurrent_writer(path, worker_id))
    except BaseException as exc:  # pragma: no cover - reported to the parent
        results.put({"worker_id": worker_id, "status": "fail", "error_type": type(exc).__name__})
        return
    results.put({"worker_id": worker_id, "status": "pass"})


async def _hold_write_lock(
    path: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    database = AsyncSQLiteDatabase(path, coordination_timeout_seconds=10.0)
    async with database.transaction(write_lock=True):
        entered.set()
        await asyncio.to_thread(release.wait)


def _lock_holder_process(
    path: str,
    entered: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
) -> None:
    asyncio.run(_hold_write_lock(path, entered, release))


async def _try_contended_write(path: str, results: multiprocessing.queues.Queue) -> None:
    database = AsyncSQLiteDatabase(path, coordination_timeout_seconds=0.15)
    try:
        async with database.transaction(write_lock=True) as connection:
            await connection.execute(
                "INSERT INTO lock_recovery (operation_id, worker_id) VALUES (?, ?)",
                ("contender", 1),
            )
    except CoordinationTimeoutError:
        results.put({"status": "coordination_timeout"})
        return
    results.put({"status": "unexpected_commit"})


def _lock_contender_process(path: str, results: multiprocessing.queues.Queue) -> None:
    asyncio.run(_try_contended_write(path, results))


async def _commit_recovery_write(path: str) -> None:
    database = AsyncSQLiteDatabase(path, coordination_timeout_seconds=2.0)
    async with database.transaction(write_lock=True) as connection:
        await connection.execute(
            "INSERT INTO lock_recovery (operation_id, worker_id) VALUES (?, ?)",
            ("recovered", 2),
        )


def _recovery_writer_process(path: str) -> None:
    asyncio.run(_commit_recovery_write(path))


async def _write_then_crash(path: str) -> None:
    database = AsyncSQLiteDatabase(path, coordination_timeout_seconds=2.0)
    async with database.transaction(write_lock=True) as connection:
        await connection.execute(
            "INSERT INTO crash_recovery (operation_id, worker_id) VALUES (?, ?)",
            ("uncommitted", 1),
        )
        os._exit(17)


def _crash_before_commit_process(path: str) -> None:
    asyncio.run(_write_then_crash(path))


async def _commit_after_crash(path: str) -> None:
    database = AsyncSQLiteDatabase(path, coordination_timeout_seconds=2.0)
    async with database.transaction(write_lock=True) as connection:
        await connection.execute(
            "INSERT INTO crash_recovery (operation_id, worker_id) VALUES (?, ?)",
            ("recovered", 2),
        )


def _crash_recovery_process(path: str) -> None:
    asyncio.run(_commit_after_crash(path))


def _sink_worker_process(
    root: str,
    worker_id: int,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    start.wait()
    try:
        receipt = EvaluationActionSink(Path(root)).execute(
            "shared-operation",
            {"fixture": "multiprocess", "sequence": 1},
        )
    except BaseException as exc:  # pragma: no cover - reported to the parent
        results.put({"worker_id": worker_id, "status": "fail", "error_type": type(exc).__name__})
        return
    results.put(
        {
            "worker_id": worker_id,
            "status": "pass",
            "payload_hash": receipt.payload_hash,
            "receipt": receipt.receipt,
            "duplicate": receipt.duplicate,
        }
    )


def _join(process: multiprocessing.Process, *, label: str) -> int:
    process.join(_SPAWN_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise TimeoutError(f"spawned SQLite scenario timed out: {label}")
    return int(process.exitcode if process.exitcode is not None else -999)


def _queue_get(results: multiprocessing.queues.Queue, *, label: str) -> Mapping[str, Any]:
    try:
        value = results.get(timeout=5)
    except queue.Empty as exc:
        raise RuntimeError(f"spawned SQLite scenario produced no result: {label}") from exc
    if not isinstance(value, Mapping):
        raise RuntimeError(f"spawned SQLite scenario produced invalid result: {label}")
    return value


def _table_count(path: Path, table: str, where: str | None = None) -> int:
    query = f'SELECT COUNT(*) FROM "{table}"'
    if where is not None:
        query += f" WHERE {where}"
    with sqlite3.connect(path) as connection:
        return int(connection.execute(query).fetchone()[0])


def _database_integrity(path: Path) -> tuple[str, str]:
    with sqlite3.connect(path, timeout=5) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    return integrity, journal


def run_matrix(root: Path) -> Matrix:
    """Execute the exact supported-topology matrix against isolated durable files."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    coordination_path = root / "coordination.sqlite3"
    sink_root = root / "action-sink"
    asyncio.run(_initialize_coordination_database(str(coordination_path)))
    context = multiprocessing.get_context("spawn")

    # Four independent processes all retry the same 32 durable identities.
    writer_start = context.Event()
    writer_results = context.Queue()
    writers = [
        context.Process(
            target=_concurrent_writer_process,
            args=(str(coordination_path), worker_id, writer_start, writer_results),
            name=f"sqlite-writer-{worker_id}",
        )
        for worker_id in range(_WORKER_COUNT)
    ]
    for process in writers:
        process.start()
    writer_start.set()
    writer_exit_codes = [_join(process, label=process.name) for process in writers]
    writer_observations = [
        dict(_queue_get(writer_results, label=f"writer-{index}")) for index in range(_WORKER_COUNT)
    ]
    writer_rows = _table_count(coordination_path, "durable_operations")
    concurrent_pass = (
        writer_exit_codes == [0] * _WORKER_COUNT
        and all(item.get("status") == "pass" for item in writer_observations)
        and writer_rows == _OPERATION_COUNT
    )

    # One process holds BEGIN IMMEDIATE. A bounded contender must fail closed,
    # and a fresh process must commit after the lock owner releases.
    lock_entered = context.Event()
    lock_release = context.Event()
    holder = context.Process(
        target=_lock_holder_process,
        args=(str(coordination_path), lock_entered, lock_release),
        name="sqlite-lock-holder",
    )
    holder.start()
    contender = None
    try:
        if not lock_entered.wait(_SPAWN_TIMEOUT_SECONDS):
            holder.terminate()
            holder.join(5)
            raise TimeoutError("SQLite lock holder did not enter its transaction")
        contender_results = context.Queue()
        contender = context.Process(
            target=_lock_contender_process,
            args=(str(coordination_path), contender_results),
            name="sqlite-lock-contender",
        )
        contender.start()
        contender_exit = _join(contender, label=contender.name)
        contender_observation = dict(_queue_get(contender_results, label=contender.name))
        before_release_rows = _table_count(coordination_path, "lock_recovery")
    finally:
        lock_release.set()
        if contender is not None and contender.is_alive():
            contender.terminate()
            contender.join(5)
        holder_exit = _join(holder, label=holder.name)
    recovery = context.Process(
        target=_recovery_writer_process,
        args=(str(coordination_path),),
        name="sqlite-lock-recovery",
    )
    recovery.start()
    recovery_exit = _join(recovery, label=recovery.name)
    lock_rows = _table_count(coordination_path, "lock_recovery")
    contention_pass = (
        contender_exit == holder_exit == recovery_exit == 0
        and contender_observation.get("status") == "coordination_timeout"
        and before_release_rows == 0
        and lock_rows == 1
    )

    # Abruptly terminate inside an uncommitted write transaction, then rebuild
    # the wrapper in a new process and commit a clean recovery row.
    crasher = context.Process(
        target=_crash_before_commit_process,
        args=(str(coordination_path),),
        name="sqlite-crash-before-commit",
    )
    crasher.start()
    crash_exit = _join(crasher, label=crasher.name)
    uncommitted_rows = _table_count(
        coordination_path, "crash_recovery", "operation_id = 'uncommitted'"
    )
    crash_recovery = context.Process(
        target=_crash_recovery_process,
        args=(str(coordination_path),),
        name="sqlite-crash-recovery",
    )
    crash_recovery.start()
    crash_recovery_exit = _join(crash_recovery, label=crash_recovery.name)
    recovery_rows = _table_count(coordination_path, "crash_recovery")
    crash_pass = (
        crash_exit == 17
        and crash_recovery_exit == 0
        and uncommitted_rows == 0
        and recovery_rows == 1
    )

    # Exercise the actual campaign action sink: one operation, one durable
    # marker, identical result for all concurrent deliveries and after restart.
    sink_start = context.Event()
    sink_results = context.Queue()
    sink_workers = [
        context.Process(
            target=_sink_worker_process,
            args=(str(sink_root), worker_id, sink_start, sink_results),
            name=f"sqlite-sink-{worker_id}",
        )
        for worker_id in range(_WORKER_COUNT)
    ]
    for process in sink_workers:
        process.start()
    sink_start.set()
    sink_exit_codes = [_join(process, label=process.name) for process in sink_workers]
    sink_observations = [
        dict(_queue_get(sink_results, label=f"sink-{index}")) for index in range(_WORKER_COUNT)
    ]
    restarted_sink = EvaluationActionSink(sink_root)
    restored = restarted_sink.lookup("shared-operation")
    conflict_rejected = False
    try:
        restarted_sink.execute("shared-operation", {"fixture": "conflict", "sequence": 2})
    except ActionPayloadConflictError:
        conflict_rejected = True
    result_pairs = {
        (str(item.get("payload_hash")), str(item.get("receipt")))
        for item in sink_observations
        if item.get("status") == "pass"
    }
    sink_pass = (
        sink_exit_codes == [0] * _WORKER_COUNT
        and len(sink_observations) == _WORKER_COUNT
        and all(item.get("status") == "pass" for item in sink_observations)
        and len(result_pairs) == 1
        and restarted_sink.marker_count() == 1
        and conflict_rejected
        and restored is not None
        and (restored.payload_hash, restored.receipt) == next(iter(result_pairs), (None, None))
    )

    coordination_integrity, coordination_journal = _database_integrity(coordination_path)
    sink_integrity, sink_journal = _database_integrity(restarted_sink.database_path)
    integrity_pass = (
        coordination_integrity == "ok"
        and sink_integrity == "ok"
        and coordination_journal == "wal"
        and sink_journal == "wal"
    )

    scenarios = {
        "concurrent_unique_writes": {
            "status": "pass" if concurrent_pass else "fail",
            "worker_count": _WORKER_COUNT,
            "expected_rows": _OPERATION_COUNT,
            "actual_rows": writer_rows,
            "duplicate_attempts": (_WORKER_COUNT * _OPERATION_COUNT) - _OPERATION_COUNT,
        },
        "bounded_lock_contention": {
            "status": "pass" if contention_pass else "fail",
            "timed_out_fail_closed": contender_observation.get("status") == "coordination_timeout"
            and before_release_rows == 0,
            "retry_after_release_committed": recovery_exit == 0 and lock_rows == 1,
            "actual_rows": lock_rows,
        },
        "crash_before_commit": {
            "status": "pass" if crash_pass else "fail",
            "crashed_exit_code": crash_exit,
            "uncommitted_rows_after_restart": uncommitted_rows,
            "recovery_rows": recovery_rows,
        },
        "idempotent_sink_restart": {
            "status": "pass" if sink_pass else "fail",
            "worker_count": _WORKER_COUNT,
            "effect_markers": restarted_sink.marker_count(),
            "matching_results": sum(item.get("status") == "pass" for item in sink_observations),
            "payload_conflict_rejected": conflict_rejected,
            "restart_lookup_matched": restored is not None
            and (restored.payload_hash, restored.receipt) == next(iter(result_pairs), (None, None)),
        },
        "integrity": {
            "status": "pass" if integrity_pass else "fail",
            "coordination_integrity": coordination_integrity,
            "sink_integrity": sink_integrity,
            "coordination_journal_mode": coordination_journal,
            "sink_journal_mode": sink_journal,
        },
    }
    return {
        "all_passed": all(item["status"] == "pass" for item in scenarios.values()),
        "process_start_method": context.get_start_method(),
        "provider_calls": 0,
        "external_network_calls": 0,
        "scenarios": scenarios,
        "database_paths": {
            "coordination": str(coordination_path),
            "action_sink": str(restarted_sink.database_path),
        },
    }


def _run_matrix_subprocess(
    work_root: Path, output: Path
) -> tuple[Matrix, subprocess.CompletedProcess[str]]:
    argv = [
        sys.executable,
        "-m",
        "release.live_evaluation.sqlite_multiprocess_checkpoint",
        "--run-matrix-root",
        str(work_root),
        "--output",
        str(output),
    ]
    completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    if not output.is_file():
        detail = completed.stderr.strip()[-4000:] or "(no stderr)"
        raise RuntimeError(
            "SQLite multiprocess subprocess did not persist its result "
            f"(exit {completed.returncode}): {detail}"
        )
    matrix = json.loads(output.read_text())
    if not isinstance(matrix, dict):
        raise RuntimeError("SQLite multiprocess subprocess result is invalid")
    return matrix, completed


def build_checkpoint(
    *,
    root: Path,
    repository: Path,
    matrix_runner: MatrixRunner | None = None,
) -> Path:
    """Run, classify, and checksum-seal the supported SQLite topology proof."""
    repository = repository.resolve(strict=True)
    root = root.resolve()
    store = EvidenceStore(root)
    work_parent = root.parent
    work_parent.mkdir(parents=True, exist_ok=True)
    work_root = Path(tempfile.mkdtemp(prefix=f".{root.name}-work-", dir=work_parent))
    # run_matrix requires an absent destination so its creation is an observable boundary.
    work_root.rmdir()
    output = work_parent / f".{root.name}-matrix.json"
    output.unlink(missing_ok=True)
    try:
        if matrix_runner is None:
            matrix, completed = _run_matrix_subprocess(work_root, output)
            argv = [str(item) for item in completed.args]
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        else:
            matrix = matrix_runner(work_root)
            completed = None
            argv = ["in-process-matrix-runner", "<isolated-native-filesystem>"]
            exit_code = 0 if matrix.get("all_passed") is True else 1
            stdout = json.dumps(
                {"all_passed": matrix.get("all_passed"), "scenarios": matrix.get("scenarios")},
                sort_keys=True,
            )
            stderr = ""

        store.record_command(
            sequence=1,
            name="sqlite-multiprocess-matrix",
            argv=argv,
            working_directory=repository,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )
        command_ref = "commands/0001-sqlite-multiprocess-matrix.json"

        if output.is_file():
            store.ingest_artifact(output, "console/sqlite-multiprocess-matrix.json")
        else:
            safe_output = work_parent / f".{root.name}-safe-matrix.json"
            safe_output.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n")
            try:
                store.ingest_artifact(safe_output, "console/sqlite-multiprocess-matrix.json")
            finally:
                safe_output.unlink(missing_ok=True)
        evidence = [command_ref, "console/sqlite-multiprocess-matrix.json"]

        paths = matrix.get("database_paths")
        if isinstance(paths, Mapping):
            coordination = paths.get("coordination")
            action_sink = paths.get("action_sink")
            if isinstance(coordination, str) and Path(coordination).is_file():
                store.snapshot_sqlite(Path(coordination), name="coordination.sqlite3")
                evidence.append("database-snapshots/coordination.sqlite3")
            if isinstance(action_sink, str) and Path(action_sink).is_file():
                store.snapshot_sqlite(Path(action_sink), name="action-sink.sqlite3")
                evidence.append("database-snapshots/action-sink.sqlite3")

        scenarios = matrix.get("scenarios")
        failed = sorted(
            str(name)
            for name, value in (scenarios.items() if isinstance(scenarios, Mapping) else ())
            if not isinstance(value, Mapping) or value.get("status") != "pass"
        )
        passed = matrix.get("all_passed") is True and not failed and exit_code == 0
        event_id = store.append_event(
            "campaign.sqlite.multiprocess_matrix",
            {
                "scope": "native_single_host_filesystem",
                "process_start_method": matrix.get("process_start_method"),
                "scenarios": dict(scenarios) if isinstance(scenarios, Mapping) else {},
                "result": "pass" if passed else "fail",
                "provider_calls": matrix.get("provider_calls", 0),
                "external_network_calls": matrix.get("external_network_calls", 0),
                "docker_bind_mount_proven": False,
                "cross_host_proven": False,
            },
        )
        evidence.append(f"events.ndjson#{event_id}")

        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        store.write_manifest(
            {
                "schema_version": 1,
                "campaign_id": "evaluation-studio-v1",
                "checkpoint": root.name,
                "revision": revision,
                "dirty_tree_hash": dirty_tree_hash(repository),
                "scope": "native_single_host_filesystem",
                "process_start_method": matrix.get("process_start_method"),
                "live_service_mutated": False,
                "d012_touched": False,
                "provider_calls_performed": matrix.get("provider_calls", 0),
                "external_network_calls": matrix.get("external_network_calls", 0),
                "docker_bind_mount_proven": False,
                "cross_host_proven": False,
            }
        )
        note = None if passed else "Non-passing scenarios: " + ", ".join(failed or ["matrix"])
        store.finalize_bundle(
            acceptance=(
                AcceptanceCriterion(
                    "SQLITE-NATIVE-SINGLE-HOST-MULTIPROCESS",
                    "pass" if passed else "fail",
                    tuple(evidence),
                    note,
                ),
                AcceptanceCriterion(
                    "SQLITE-DOCKER-BIND-MULTIPROCESS",
                    "blocked",
                    (f"events.ndjson#{event_id}",),
                    "Docker Desktop bind-mounted host files are outside this proof. The campaign "
                    "previously observed corruption and disk I/O failure in that topology.",
                ),
                AcceptanceCriterion(
                    "SQLITE-CROSS-HOST-COORDINATION",
                    "blocked",
                    (f"events.ndjson#{event_id}",),
                    "SQLite is not a cross-host coordination service; use PostgreSQL or another "
                    "shared transactional repository for multi-host writers.",
                ),
            ),
            report_markdown=(
                "# SQLite multiprocess persistence and recovery checkpoint\n\n"
                "The supported native single-host matrix finished with result "
                f"`{'pass' if passed else 'fail'}`. "
                "It used real spawned processes to prove concurrent unique writes, a bounded "
                "write-lock timeout with no partial mutation, retry after release, rollback after "
                "abrupt exit before commit, restart recovery, one durable local action marker for "
                "concurrent duplicate deliveries, payload-conflict refusal, and SQLite integrity. "
                "No live service, D-012 state, provider, or external network was touched.\n\n"
                "## Adversarial review\n\n"
                "The strongest objection is deployment topology: native single-host locking does "
                "not make SQLite safe over Docker Desktop bind mounts or across hosts. Those "
                "scopes remain blocked. The simpler safe operating model is one SQLite writer on "
                "a native filesystem; use PostgreSQL before adding container or host writers.\n"
            ),
        )
        return root
    finally:
        output.unlink(missing_ok=True)
        shutil.rmtree(work_root, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--run-matrix-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.run_matrix_root is not None:
        if args.output is None:
            parser.error("--output is required with --run-matrix-root")
        matrix = run_matrix(args.run_matrix_root)
        args.output.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n")
        print("SQLite multiprocess matrix: " + ("pass" if matrix["all_passed"] else "fail"))
        return 0 if matrix["all_passed"] else 1
    if args.root is None or args.repository is None:
        parser.error("--root and --repository are required")
    build_checkpoint(root=args.root, repository=args.repository)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["EvidenceStore", "build_checkpoint", "run_matrix"]
