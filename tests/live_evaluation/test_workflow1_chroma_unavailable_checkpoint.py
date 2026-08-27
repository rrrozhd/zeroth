from __future__ import annotations

import importlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from release.live_evaluation.evidence import EvidenceStore


def _module() -> Any:
    module_name = "release.live_evaluation.workflow1_chroma_unavailable_checkpoint"
    assert importlib.util.find_spec(module_name) is not None
    return importlib.import_module(module_name)


def _audit(
    *,
    sequence: int,
    node_id: str,
    status: str,
    digest: str,
    previous_digest: str | None,
) -> dict[str, object]:
    return {
        "audit_id": f"audit-{sequence}",
        "run_id": "04e6af617a55429b987ad9b2aacdf848",
        "node_id": node_id,
        "chain_sequence": sequence,
        "status": status,
        "attempt": 1,
        "record_digest": digest,
        "previous_record_digest": previous_digest,
        "record_signature": f"hmac-sha256:signature-{sequence}",
        "signing_key_id": "evaluation-studio-audit-v1",
        "signing_algorithm": "HS256",
        "cost_usd": None if node_id == "retrieve" else 0.0,
        "cost_event_id": None,
        "execution_metadata": {
            "provider_request_id": None,
            "raw_connector_diagnostic": "must not be sealed",
        },
        "input_snapshot": {"query": "must not be sealed"},
        "error": "deterministic evaluation connector unavailable" if status == "failed" else None,
    }


def _responses() -> dict[tuple[str, str], dict[str, object]]:
    module = _module()
    digests = [character * 64 for character in ("a", "b", "c")]
    audits = [
        _audit(
            sequence=index,
            node_id=node_id,
            status=status,
            digest=digests[index - 1],
            previous_digest=None if index == 1 else digests[index - 2],
        )
        for index, (node_id, status) in enumerate(
            (("request", "completed"), ("revision-loop", "completed"), ("retrieve", "failed")),
            start=1,
        )
    ]
    run = {
        "run_id": module.RUN_ID,
        "status": "failed",
        "deployment_ref": module.DEPLOYMENT,
        "graph_version_ref": module.GRAPH,
        "thread_id": "thread-workflow1-negative",
        "tenant_id": module.TENANT,
        "campaign_id": module.TENANT,
        "failure_state": {
            "reason": "node_execution_failed",
            "message": "deterministic evaluation connector unavailable",
            "details": {"connector": "chroma", "diagnostic": "must not be sealed"},
        },
        "terminal_output": {"raw": "must not be sealed"},
        "traversal": {"node_visit_counts": {"request": 1, "revision-loop": 1, "retrieve": 1}},
    }
    verification = {
        "scope": f"run:{module.RUN_ID}",
        "verified": True,
        "signature_verified": True,
        "record_count": 3,
        "failed_audit_id": None,
        "error": None,
        "signing_key_id": "evaluation-studio-audit-v1",
        "unsigned_record_count": 0,
    }
    deployment_verification = {
        "scope": f"deployment:{module.DEPLOYMENT}",
        "verified": True,
        "signature_verified": True,
        "record_count": 12,
        "failed_audit_id": None,
        "error": None,
        "signing_key_id": "evaluation-studio-audit-v1",
        "unsigned_record_count": 0,
    }
    return {
        ("GET", "/health"): {
            "status": "ok",
            "deployment_ref": module.DEPLOYMENT,
            "deployment_version": 6,
            "graph_version_ref": module.GRAPH,
            "campaign_id": module.TENANT,
            "debug": "must not be sealed",
        },
        ("GET", f"/v1/runs/{module.RUN_ID}"): run,
        ("GET", f"/v1/runs/{module.RUN_ID}/timeline"): {
            "deployment_ref": module.DEPLOYMENT,
            "run_id": module.RUN_ID,
            "entries": audits,
        },
        ("GET", f"/v1/runs/{module.RUN_ID}/evidence"): {
            "run": deepcopy(run),
            "audits": deepcopy(audits),
            "approvals": [],
            "summary": {
                "audit_count": 3,
                "approval_count": 0,
                "tool_call_count": 0,
                "memory_interaction_count": 0,
                "priced_call_count": 0,
                "cost_event_count": 0,
                "total_cost_usd": 0.0,
                "cost_identity_state": "not_applicable_no_priced_call",
                "reconciliation_state": "reconciled_zero_activity",
            },
            "policy_events": [],
        },
        ("POST", f"/v1/runs/{module.RUN_ID}/verify-chain"): verification,
        ("GET", f"/v1/deployments/{module.DEPLOYMENT}/audit-verification"): (
            deployment_verification
        ),
        ("GET", f"/v1/deployments/{module.DEPLOYMENT}/cost"): {
            "deployment_ref": module.DEPLOYMENT,
            "total_cost_usd": 4.25,
            "paid_spend_usd": 4.0,
            "estimated_spend_usd": 0.25,
            "unmeasured_spend_usd": 0.0,
            "active_exposure_usd": 0.0,
            "ambiguous_exposure_usd": 0.0,
            "currency": "USD",
        },
    }


def _request(
    responses: dict[tuple[str, str], dict[str, object]],
    calls: list[tuple[str, str]],
):
    def request(path: str, *, method: str = "GET") -> dict[str, object]:
        key = (method, path)
        calls.append(key)
        return deepcopy(responses[key])

    return request


def test_build_checkpoint_fetches_exact_read_only_sources_and_seals_safe_bundle(
    tmp_path: Path,
) -> None:
    module = _module()
    responses = _responses()
    calls: list[tuple[str, str]] = []
    destination = tmp_path / "checkpoint"
    screenshots = []
    for index, name in enumerate(
        ("configured", "failed", "run-detail", "run-chain", "audit-chain"), start=1
    ):
        source = tmp_path / f"{index:02d}-{name}-native-safari.jpg"
        source.write_bytes(b"\xff\xd8\xffsafe-jpeg")
        screenshots.append(source)

    result = module.build_checkpoint(
        destination=destination,
        request=_request(responses, calls),
        screenshot_sources=screenshots,
    )

    assert result == destination
    assert EvidenceStore(destination).is_sealed
    assert calls == [
        ("GET", "/health"),
        ("GET", f"/v1/runs/{module.RUN_ID}"),
        ("GET", f"/v1/runs/{module.RUN_ID}/timeline"),
        ("GET", f"/v1/runs/{module.RUN_ID}/evidence"),
        ("POST", f"/v1/runs/{module.RUN_ID}/verify-chain"),
        ("GET", f"/v1/deployments/{module.DEPLOYMENT}/audit-verification"),
        ("GET", f"/v1/deployments/{module.DEPLOYMENT}/cost"),
    ]
    expected_files = {
        "manifest.json",
        "events.ndjson",
        "acceptance.json",
        "report.md",
        "SHA256SUMS",
        "runtime/health.json",
        "runtime/run.json",
        "runtime/timeline.json",
        "runtime/run-evidence.json",
        "runtime/run-chain-verification.json",
        "runtime/deployment-audit-verification.json",
        "runtime/deployment-cost.json",
        "screenshots/01-configured-native-safari.jpg",
        "screenshots/02-failed-native-safari.jpg",
        "screenshots/03-run-detail-native-safari.jpg",
        "screenshots/04-run-chain-native-safari.jpg",
        "screenshots/05-audit-chain-native-safari.jpg",
    }
    assert {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    } == expected_files

    run = json.loads((destination / "runtime/run.json").read_text())
    assert run == {
        "campaign_id": module.TENANT,
        "deployment_ref": module.DEPLOYMENT,
        "failure_reason": "node_execution_failed",
        "graph_version_ref": module.GRAPH,
        "run_id": module.RUN_ID,
        "status": "failed",
        "tenant_id": module.TENANT,
    }
    timeline = json.loads((destination / "runtime/timeline.json").read_text())
    assert [row["node_id"] for row in timeline["entries"]] == [
        "request",
        "revision-loop",
        "retrieve",
    ]
    assert [row["cost_usd"] for row in timeline["entries"]] == [0.0, 0.0, None]
    assert all(row["record_signature_present"] is True for row in timeline["entries"])
    bundle_text = "\n".join(
        path.read_text()
        for path in destination.rglob("*")
        if path.is_file() and path.suffix.lower() not in {".jpg", ".jpeg"}
    )
    assert "must not be sealed" not in bundle_text
    assert "deterministic evaluation connector unavailable" not in bundle_text

    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert [row["criterion_id"] for row in acceptance["criteria"]] == list(module.ACCEPTED_CRITERIA)
    assert all(row["status"] == "pass" for row in acceptance["criteria"])
    assert all("events.ndjson#" in "\n".join(row["evidence"]) for row in acceptance["criteria"])
    assert all(
        any(path.startswith("screenshots/") for path in row["evidence"])
        for row in acceptance["criteria"]
    )
    report = (destination / "report.md").read_text()
    assert "cumulative deployment cost snapshot is contextual" in report
    assert "does not claim the one-shot connector fault row was consumed" in report
    assert "No provider call was made" in report

    checksum_paths = {
        line.split("  ", maxsplit=1)[1]
        for line in (destination / "SHA256SUMS").read_text().splitlines()
    }
    assert checksum_paths == expected_files - {"SHA256SUMS"}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda responses, module: responses[("GET", "/health")].__setitem__(
                "graph_version_ref", "wrong@9"
            ),
            "health identity",
        ),
        (
            lambda responses, module: responses[("GET", f"/v1/runs/{module.RUN_ID}/timeline")][
                "entries"
            ].append(  # type: ignore[union-attr]
                _audit(
                    sequence=4,
                    node_id="research",
                    status="completed",
                    digest="d" * 64,
                    previous_digest="c" * 64,
                )
            ),
            "exactly three",
        ),
        (
            lambda responses, module: responses[("GET", f"/v1/runs/{module.RUN_ID}/timeline")][
                "entries"
            ][2].__setitem__("record_signature", None),  # type: ignore[index,union-attr]
            "signed audits",
        ),
        (
            lambda responses, module: responses[("GET", f"/v1/runs/{module.RUN_ID}/evidence")][
                "summary"
            ].__setitem__("priced_call_count", 1),  # type: ignore[union-attr]
            "zero priced calls",
        ),
    ],
)
def test_invalid_checkpoint_is_rejected_before_destination_creation(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    module = _module()
    responses = _responses()
    mutation(responses, module)
    destination = tmp_path / "rejected"

    with pytest.raises(RuntimeError, match=message):
        module.build_checkpoint(
            destination=destination,
            request=_request(responses, []),
            screenshot_sources=[],
        )

    assert not destination.exists()


def test_checkpoint_acceptance_allowlist_and_identity_are_exact() -> None:
    module = _module()
    assert module.RUN_ID == "04e6af617a55429b987ad9b2aacdf848"
    assert module.DEPLOYMENT == "evaluation-studio-v1-grounded-researcher-v1"
    assert module.GRAPH == "evaluation-studio-v1-grounded-researcher@4"
    assert module.ACCEPTED_CRITERIA == ("workflow1.negative-chroma-unavailable",)
