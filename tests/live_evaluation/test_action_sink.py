from __future__ import annotations

from pathlib import Path

import pytest

from release.live_evaluation.action_sink import (
    ActionPayloadConflictError,
    ActionSinkUnavailableError,
    EvaluationActionSink,
)


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
