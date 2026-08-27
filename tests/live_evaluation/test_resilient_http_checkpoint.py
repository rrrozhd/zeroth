from __future__ import annotations

from copy import deepcopy

import pytest

from release.live_evaluation.resilient_http_checkpoint import validate_resilient_http_summary


def _summary() -> dict[str, object]:
    def audit(
        node: str, status: str, *, reason: str | None, retries: int, upstream: int | None
    ) -> dict[str, object]:
        metadata: dict[str, object] = {
            "node_kind": "http_request",
            "target_url_sha256": "a" * 64,
            "retry_count": retries,
        }
        if reason is not None:
            metadata["reason_code"] = reason
        if upstream is not None:
            metadata["upstream_status_code"] = upstream
        return {
            "audit_id": f"audit-{node}-{status}-{reason}",
            "node_id": node,
            "status": status,
            "record_signature_present": True,
            "cost_event_id": None,
            "execution_metadata": metadata,
        }

    audits = [
        audit("http-retry", "completed", reason=None, retries=2, upstream=200),
        audit(
            "http-timeout", "failed", reason="http_retry_exhausted_error", retries=2, upstream=None
        ),
        audit("http-circuit", "failed", reason="circuit_open_error", retries=0, upstream=None),
        audit("http-circuit", "completed", reason=None, retries=0, upstream=200),
    ]
    return {
        "schema_version": 1,
        "health": {"status": "ok", "deployment_ref": "http", "graph_version_ref": "http@1"},
        "runs": [
            {"run_id": f"run-{index}", "status": status}
            for index, status in enumerate(("succeeded", "failed", "failed", "failed", "succeeded"))
        ],
        "audits": audits,
        "audit_count": len(audits),
        "provider_call_count": 0,
        "cost_event_ids": [],
        "total_cost_usd": 0,
        "scenario_events": {
            "events": [
                {"sequence": 1, "scenario": "retry-then-success", "status_code": 503},
                {"sequence": 2, "scenario": "retry-then-success", "status_code": 503},
                {"sequence": 3, "scenario": "retry-then-success", "status_code": 200},
                {"sequence": 4, "scenario": "circuit", "status_code": 200},
            ],
            "recovered": True,
        },
    }


def test_summary_accepts_exact_signed_zero_provider_journey() -> None:
    result = validate_resilient_http_summary(_summary())
    assert result["priced_call_count"] == 0
    assert result["total_cost_usd"] == 0
    assert len(result["run_ids"]) == 5


@pytest.mark.parametrize("mutation", ["unsigned", "cost", "raw_url", "missing_recovery"])
def test_summary_fails_closed(mutation: str) -> None:
    value = deepcopy(_summary())
    if mutation == "unsigned":
        value["audits"][0]["record_signature_present"] = False  # type: ignore[index]
    elif mutation == "cost":
        value["total_cost_usd"] = 0.01
    elif mutation == "raw_url":
        value["audits"][0]["execution_metadata"]["unsafe"] = "http://127.0.0.1"  # type: ignore[index]
    else:
        value["scenario_events"]["recovered"] = False  # type: ignore[index]
    with pytest.raises(RuntimeError):
        validate_resilient_http_summary(value)
