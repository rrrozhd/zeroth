"""Legacy per-step retention log entries emitted alongside the manifest.

These entries predate the cleanup manifest and exist so operators' existing
queries over ``retention_audit_log`` keep working. They are best-effort by
design: a compatibility write that fails must never abort an erasure that has
already destroyed data, because the caller cannot retry what is already gone.
"""

from __future__ import annotations

import pytest

from zeroth.core.retention.models import ErasureResult
from zeroth.governance.retention.compatibility import CompatibilityLog
from zeroth.governance.retention.manifests import build_cleanup_manifest


class _RecordingLog:
    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[tuple[str, dict]] = []
        self._fail = fail

    async def record(self, *, tenant_id, action, run_id=None, reason=None, detail=None) -> str:
        if self._fail:
            raise RuntimeError("retention log unavailable")
        self.entries.append((action, dict(detail or {})))
        return f"log-{len(self.entries)}"


class _Store:
    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        return 0

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        return True


class _Eraser:
    async def delete_events_for_run(self, tenant_id, join_keys, *, idempotency_key) -> int:
        return 0


def _result(**overrides) -> ErasureResult:
    fields = {
        "run_id": "run-1",
        "tenant_id": "t1",
        "reason": "rte",
        "audits_erased": 3,
        "checkpoints_deleted": 2,
        "run_redacted": True,
        "artifacts_deleted": 4,
        "econ_events_deleted": 5,
    }
    fields.update(overrides)
    return ErasureResult(**fields)


def _manifest(*, econ_status: str = "completed", eraser: object | None = None):
    manifest = build_cleanup_manifest(
        _result(),
        ["run-1/a"],
        ["run-1", "join-1"],
        artifact_store=_Store(),
        econ_eraser=eraser if eraser is not None else _Eraser(),
    )
    manifest.operations = [
        operation.model_copy(update={"status": econ_status})
        if operation.kind == "econ"
        else operation.model_copy(update={"status": "completed"})
        for operation in manifest.operations
    ]
    return manifest


async def test_database_steps_are_logged_in_erasure_order() -> None:
    log = _RecordingLog()

    await CompatibilityLog(log=log).record_database_steps(_result())

    assert [action for action, _ in log.entries] == [
        "crypto_erase_audits",
        "erase_checkpoints",
        "redact_run",
    ]
    assert [detail for _, detail in log.entries] == [
        {"count": 3},
        {"count": 2},
        {"redacted": True},
    ]


async def test_a_failing_compatibility_write_is_swallowed() -> None:
    """Data is already destroyed by this point; raising here helps nobody."""
    await CompatibilityLog(log=_RecordingLog(fail=True)).record_database_steps(_result())


async def test_external_steps_log_artifacts_then_econ_then_completion() -> None:
    log = _RecordingLog()

    await CompatibilityLog(log=log).record_external_steps(
        _result(), _manifest(), failed=False
    )

    assert [action for action, _ in log.entries] == [
        "artifact_cleanup",
        "econ_erase",
        "erase_run_complete",
    ]
    assert log.entries[0][1] == {"count": 4}
    assert log.entries[1][1]["count"] == 5
    assert log.entries[1][1]["join_keys"] == ["run-1", "join-1"]


@pytest.mark.parametrize(
    ("econ_status", "action"),
    [
        ("completed", "econ_erase"),
        ("failed", "econ_erase_failed"),
        ("skipped", "econ_erase_skipped"),
        ("pending", "econ_erase_skipped"),
    ],
)
async def test_the_econ_action_name_follows_the_operation_status(econ_status, action) -> None:
    log = _RecordingLog()

    await CompatibilityLog(log=log).record_external_steps(
        _result(), _manifest(econ_status=econ_status), failed=False
    )

    assert [entry for entry, _ in log.entries][1] == action


async def test_a_failed_cleanup_never_logs_a_completion() -> None:
    """``erase_run_complete`` is the marker operators trust; a failure must not emit it."""
    log = _RecordingLog()

    await CompatibilityLog(log=log).record_external_steps(
        _result(), _manifest(econ_status="failed"), failed=True
    )

    assert "erase_run_complete" not in [action for action, _ in log.entries]


async def test_the_completion_detail_carries_the_whole_result() -> None:
    log = _RecordingLog()

    await CompatibilityLog(log=log).record_external_steps(
        _result(), _manifest(), failed=False
    )

    completion = dict(log.entries[-1][1])
    assert completion == {
        "audits_erased": 3,
        "checkpoints_deleted": 2,
        "run_redacted": True,
        "artifacts_deleted": 4,
        "econ_events_deleted": 5,
        "external_cleanup_status": "pending",
        "authorization_log_id": None,
        "retry_log_id": None,
    }
