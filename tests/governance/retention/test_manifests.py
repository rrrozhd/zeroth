"""Manifest construction and projection, isolated from the erasure service.

These functions decide what external cleanup is owed and how a partially
finished manifest reads back to a caller. Both are pure, so they can be pinned
directly rather than through a wired database.
"""

from __future__ import annotations

import pytest

from zeroth.core.retention.cleanup_manifest import CleanupManifest, operation_id
from zeroth.core.retention.models import ErasureResult
from zeroth.governance.retention.manifests import (
    build_cleanup_manifest,
    manifest_complete,
    result_from_manifest,
)


class _Store:
    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        return 0

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        return True


class _Eraser:
    async def delete_events_for_run(self, tenant_id, join_keys, *, idempotency_key) -> int:
        return 0


def _result(**overrides) -> ErasureResult:
    fields = {"run_id": "run-1", "tenant_id": "t1", "reason": "rte"}
    fields.update(overrides)
    return ErasureResult(**fields)


def test_manifest_lists_prefix_then_keys_then_econ() -> None:
    """Operation order is the execution order: prefix sweep, keys, econ last."""
    manifest = build_cleanup_manifest(
        _result(audits_erased=2, checkpoints_deleted=1, run_redacted=True),
        ["run-1/a", "run-1/b"],
        ["run-1", "join-1"],
        artifact_store=_Store(),
        econ_eraser=_Eraser(),
    )

    assert [operation.kind for operation in manifest.operations] == [
        "artifact_prefix",
        "artifact_key",
        "artifact_key",
        "econ",
    ]
    assert [operation.artifact_key for operation in manifest.operations[1:3]] == [
        "run-1/a",
        "run-1/b",
    ]
    assert manifest.database_result.audits_erased == 2
    assert manifest.database_result.checkpoints_deleted == 1
    assert manifest.database_result.run_redacted is True
    assert all(operation.status == "pending" for operation in manifest.operations)


def test_operation_ids_are_stable_across_rebuilds() -> None:
    """The idempotency key is derived, never generated, so a retry reuses it."""
    manifest = build_cleanup_manifest(
        _result(),
        ["run-1/a"],
        ["run-1"],
        artifact_store=_Store(),
        econ_eraser=_Eraser(),
    )

    assert manifest.operations[0].operation_id == operation_id(
        "t1", "run-1", "artifact_prefix", "run-1"
    )
    assert manifest.operations[1].operation_id == operation_id(
        "t1", "run-1", "artifact_key", "run-1/a"
    )
    assert manifest.operations[2].operation_id == operation_id("t1", "run-1", "econ", "run-1")


@pytest.mark.parametrize(
    ("store", "eraser", "expected"),
    [
        (None, None, ["skipped", "skipped", "skipped"]),
        (_Store(), None, ["pending", "pending", "skipped"]),
        (None, _Eraser(), ["skipped", "skipped", "pending"]),
    ],
)
def test_absent_collaborators_mark_their_operations_skipped(store, eraser, expected) -> None:
    """An unwired surface is skipped, not left pending forever."""
    manifest = build_cleanup_manifest(
        _result(),
        ["run-1/a"],
        ["run-1"],
        artifact_store=store,
        econ_eraser=eraser,
    )

    assert [operation.status for operation in manifest.operations] == expected


def test_a_store_without_prefix_cleanup_skips_only_the_prefix_sweep() -> None:
    """A store that cannot sweep a prefix still gets its per-key deletes."""

    class _KeyOnlyStore:
        async def delete(self, key: str, *, idempotency_key: str) -> bool:
            return True

    manifest = build_cleanup_manifest(
        _result(),
        ["run-1/a"],
        ["run-1"],
        artifact_store=_KeyOnlyStore(),
        econ_eraser=_Eraser(),
    )

    assert manifest.operations[0].status == "skipped"
    assert manifest.operations[1].status == "pending"


def _manifest(statuses: list[str], **counts) -> CleanupManifest:
    manifest = build_cleanup_manifest(
        _result(**counts),
        ["run-1/a"],
        ["run-1"],
        artifact_store=_Store(),
        econ_eraser=_Eraser(),
    )
    manifest.operations = [
        operation.model_copy(update={"status": status})
        for operation, status in zip(manifest.operations, statuses, strict=True)
    ]
    return manifest


@pytest.mark.parametrize(
    ("statuses", "complete"),
    [
        (["completed", "completed", "completed"], True),
        (["completed", "skipped", "skipped"], True),
        (["completed", "pending", "completed"], False),
        (["completed", "failed", "completed"], False),
        (["in_progress", "completed", "completed"], False),
    ],
)
def test_manifest_completion_accepts_only_completed_and_skipped(statuses, complete) -> None:
    assert manifest_complete(_manifest(statuses)) is complete


@pytest.mark.parametrize(
    ("statuses", "status"),
    [
        (["completed", "completed", "completed"], "complete"),
        (["completed", "skipped", "skipped"], "complete"),
        (["completed", "failed", "completed"], "failed"),
        (["completed", "pending", "completed"], "pending"),
        # A failure anywhere outranks work still pending elsewhere.
        (["failed", "pending", "completed"], "failed"),
    ],
)
def test_result_status_summarizes_the_whole_manifest(statuses, status) -> None:
    result = result_from_manifest(
        _manifest(statuses),
        authorization_log_id="log-1",
        retry_log_id="log-2",
    )

    assert result.external_cleanup_status == status
    assert result.authorization_log_id == "log-1"
    assert result.retry_log_id == "log-2"


def test_result_sums_artifact_counts_and_reports_econ_separately() -> None:
    manifest = _manifest(
        ["completed", "completed", "completed"],
        audits_erased=3,
        checkpoints_deleted=2,
        run_redacted=True,
    )
    manifest.operations = [
        manifest.operations[0].model_copy(update={"deleted_count": 4}),
        manifest.operations[1].model_copy(update={"deleted_count": 1}),
        manifest.operations[2].model_copy(update={"deleted_count": 7}),
    ]

    result = result_from_manifest(
        manifest,
        authorization_log_id="log-1",
        retry_log_id=None,
    )

    assert result.artifacts_deleted == 5  # prefix sweep + per-key delete
    assert result.econ_events_deleted == 7
    assert result.audits_erased == 3
    assert result.checkpoints_deleted == 2
    assert result.run_redacted is True


def test_a_skipped_econ_operation_reports_no_count_rather_than_zero() -> None:
    """``None`` means the econ hook was never wired; ``0`` means it deleted nothing."""
    manifest = _manifest(["completed", "completed", "skipped"])

    result = result_from_manifest(manifest, authorization_log_id="log-1", retry_log_id=None)

    assert result.econ_events_deleted is None


def test_forced_status_overrides_the_derived_one() -> None:
    """A live claim held by another worker reports pending regardless of progress."""
    manifest = _manifest(["completed", "completed", "completed"])

    result = result_from_manifest(
        manifest,
        authorization_log_id="log-1",
        retry_log_id="log-2",
        force_status="pending",
    )

    assert result.external_cleanup_status == "pending"
