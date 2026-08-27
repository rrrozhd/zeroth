from __future__ import annotations

from copy import deepcopy

from release.live_evaluation.quality_verdict_checkpoint import compare_snapshots


def _snapshot() -> dict:
    return {
        "run_id": "run-1",
        "api": {
            "run": {"run_id": "run-1", "status": "succeeded"},
            "unit_economics": {
                "total_cost_usd": 0.0,
                "terminal_cost_usd": 0.0,
                "failure_tax_usd": 0.0,
                "runs_with_cost": 0,
                "quality": {"labeled_terminal_runs": 0, "state": "not_configured"},
            },
        },
        "database": {
            "run_execution_digest": "run-digest",
            "run_metadata": {"existing": "kept"},
            "thread_digest": "thread-digest",
            "checkpoints_digest": "checkpoint-digest",
            "audits_digest": "audit-digest",
            "operations_digest": "operation-digest",
            "action_markers_digest": "marker-digest",
            "economics_tables": {
                "execution_events": {"count": 1, "digest": "events"},
                "cost_reservations": {"count": 1, "digest": "reservations"},
            },
        },
    }


def test_compare_snapshots_accepts_only_the_quality_annotation() -> None:
    before = _snapshot()
    after = deepcopy(before)
    after["database"]["run_metadata"] = {
        "existing": "kept",
        "quality_verdict": {
            "verdict": "good",
            "source": "human:console",
            "attached_at": "2026-08-24T22:00:00Z",
        },
    }
    after["api"]["unit_economics"]["quality"] = {
        "labeled_terminal_runs": 1,
        "state": "below_coverage_floor",
    }

    result = compare_snapshots(before, after, run_id="run-1")

    assert result["passed"] is True
    assert all(result["checks"].values())


def test_compare_snapshots_rejects_a_checkpoint_rewrite() -> None:
    before = _snapshot()
    after = deepcopy(before)
    after["database"]["run_metadata"] = {
        "existing": "kept",
        "quality_verdict": {
            "verdict": "good",
            "source": "human:console",
            "attached_at": "2026-08-24T22:00:00Z",
        },
    }
    after["database"]["checkpoints_digest"] = "rewritten"
    after["api"]["unit_economics"]["quality"] = {
        "labeled_terminal_runs": 1,
        "state": "below_coverage_floor",
    }

    result = compare_snapshots(before, after, run_id="run-1")

    assert result["passed"] is False
    assert result["checks"]["checkpoints_unchanged"] is False
