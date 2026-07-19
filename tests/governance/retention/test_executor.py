"""Running an authorized manifest's external cleanup under a live claim.

The executor is the only place that touches surfaces outside the erasure
database. Three properties matter and are pinned here: operations run in
manifest order, a failure is recorded rather than propagated so the operations
behind it still run, and the claim lease is heartbeated while an operation is
in flight -- otherwise a slow delete outlives its own claim and its progress is
fenced out.
"""

from __future__ import annotations

import asyncio

import pytest

from zeroth.core.retention.models import ErasureResult
from zeroth.governance.retention.executor import CleanupExecutor
from zeroth.governance.retention.manifests import build_cleanup_manifest


class _Claims:
    """Records the fenced writes the executor makes, without a database."""

    def __init__(self) -> None:
        self.deltas: list[tuple[str, str]] = []
        self.heartbeats = 0
        self.terminal: tuple[str, bool] | None = None

    async def record_operation_delta(self, log_id, claim_id, generation, operation) -> str:
        self.deltas.append((operation.kind, operation.status))
        return "delta-log"

    async def record_heartbeat(self, *, authorization_log_id, claim_id, generation, tenant_id, run_id) -> str:
        self.heartbeats += 1
        return "heartbeat-log"

    async def record_terminal(self, log_id, claim_id, generation, manifest, *, failed) -> str:
        self.terminal = (log_id, failed)
        return "terminal-log"


class _Compatibility:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def record_external_steps(self, result, manifest, *, failed) -> None:
        self.calls.append(failed)


class _Store:
    def __init__(self, *, fail_prefix: bool = False) -> None:
        self.fail_prefix = fail_prefix
        self.keys: list[tuple[str, str]] = []
        self.prefixes: list[tuple[str, str]] = []

    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        if self.fail_prefix:
            raise RuntimeError("prefix sweep unavailable")
        self.prefixes.append((run_id, idempotency_key))
        return 3

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        self.keys.append((key, idempotency_key))
        return True


class _Eraser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], str]] = []

    async def delete_events_for_run(self, tenant_id, join_keys, *, idempotency_key) -> int:
        self.calls.append((tenant_id, list(join_keys), idempotency_key))
        return 9


def _manifest(store, eraser, keys=("run-1/a",)):
    return build_cleanup_manifest(
        ErasureResult(run_id="run-1", tenant_id="t1", reason="rte"),
        list(keys),
        ["run-1"],
        artifact_store=store,
        econ_eraser=eraser,
    )


def _executor(claims, compatibility, store, eraser, *, lease_seconds: float = 30.0):
    return CleanupExecutor(
        claims=claims,
        compatibility=compatibility,
        artifact_store=store,
        econ_eraser=eraser,
        lease_seconds=lease_seconds,
    )


async def test_operations_run_in_manifest_order_with_paired_deltas() -> None:
    claims, compatibility = _Claims(), _Compatibility()
    store, eraser = _Store(), _Eraser()

    terminal = await _executor(claims, compatibility, store, eraser).execute_claimed(
        authorization_log_id="auth",
        claim_id="claim-1",
        generation=1,
        manifest=_manifest(store, eraser),
    )

    assert claims.deltas == [
        ("artifact_prefix", "in_progress"),
        ("artifact_prefix", "completed"),
        ("artifact_key", "in_progress"),
        ("artifact_key", "completed"),
        ("econ", "in_progress"),
        ("econ", "completed"),
    ]
    assert claims.terminal == ("auth", False)
    assert terminal == "terminal-log"
    assert compatibility.calls == [False]


async def test_each_operation_forwards_its_own_idempotency_key() -> None:
    """The key is the operation id, so a retry of a half-done sweep deletes nothing twice."""
    claims, compatibility = _Claims(), _Compatibility()
    store, eraser = _Store(), _Eraser()
    manifest = _manifest(store, eraser)

    await _executor(claims, compatibility, store, eraser).execute_claimed(
        authorization_log_id="auth",
        claim_id="claim-1",
        generation=1,
        manifest=manifest,
    )

    by_kind = {operation.kind: operation.operation_id for operation in manifest.operations}
    assert store.prefixes == [("run-1", by_kind["artifact_prefix"])]
    assert store.keys == [("run-1/a", by_kind["artifact_key"])]
    assert eraser.calls == [("t1", ["run-1"], by_kind["econ"])]


async def test_already_finished_operations_are_not_re_run() -> None:
    claims, compatibility = _Claims(), _Compatibility()
    store, eraser = _Store(), _Eraser()
    manifest = _manifest(store, eraser)
    manifest.operations = [
        operation.model_copy(update={"status": "completed"})
        if operation.kind != "econ"
        else operation
        for operation in manifest.operations
    ]

    await _executor(claims, compatibility, store, eraser).execute_claimed(
        authorization_log_id="auth",
        claim_id="claim-1",
        generation=1,
        manifest=manifest,
    )

    assert claims.deltas == [("econ", "in_progress"), ("econ", "completed")]
    assert store.prefixes == []
    assert store.keys == []


async def test_a_failing_operation_is_recorded_and_the_rest_still_run() -> None:
    claims, compatibility = _Claims(), _Compatibility()
    store, eraser = _Store(fail_prefix=True), _Eraser()

    await _executor(claims, compatibility, store, eraser).execute_claimed(
        authorization_log_id="auth",
        claim_id="claim-1",
        generation=1,
        manifest=_manifest(store, eraser),
    )

    assert claims.deltas == [
        ("artifact_prefix", "in_progress"),
        ("artifact_prefix", "failed"),
        ("artifact_key", "in_progress"),
        ("artifact_key", "completed"),
        ("econ", "in_progress"),
        ("econ", "completed"),
    ]
    assert claims.terminal == ("auth", True)
    assert compatibility.calls == [True]
    assert eraser.calls != []


async def test_a_long_operation_heartbeats_until_it_finishes() -> None:
    """Without this the lease expires mid-delete and the worker fences itself out."""
    claims, compatibility = _Claims(), _Compatibility()
    eraser = _Eraser()

    class _SlowStore(_Store):
        async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
            await asyncio.sleep(0.25)
            return 1

    store = _SlowStore()
    # A 0.15s lease heartbeats every 0.05s, so a 0.25s operation needs several.
    await _executor(claims, compatibility, store, eraser, lease_seconds=0.15).execute_claimed(
        authorization_log_id="auth",
        claim_id="claim-1",
        generation=1,
        manifest=_manifest(store, eraser),
    )

    assert claims.heartbeats >= 2


async def test_a_cancelled_operation_does_not_leave_the_task_running() -> None:
    """The claim is released on abort, so an orphaned delete would race its successor."""
    claims, compatibility = _Claims(), _Compatibility()
    eraser = _Eraser()
    started = asyncio.Event()

    class _HangingStore(_Store):
        async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
            started.set()
            await asyncio.sleep(30)
            return 1

    store = _HangingStore()
    executor = _executor(claims, compatibility, store, eraser, lease_seconds=0.1)
    task = asyncio.create_task(
        executor.execute_claimed(
            authorization_log_id="auth",
            claim_id="claim-1",
            generation=1,
            manifest=_manifest(store, eraser),
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Nothing left pending: the inner operation task was cancelled and awaited.
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
