"""Collect sanitized runtime proof for the Workflow 1 provider-fault matrix."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .provider_free_composed import HttpFixtureClient
from .workflow1_provider_faults_live import EXPECTED_MODES, validate_provider_fault_summary


def _object(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value


def _get(request: HttpFixtureClient, path: str, *, label: str) -> dict[str, Any]:
    response = request("GET", path, None)
    if response.status_code != 200:
        raise RuntimeError(f"{label} returned {response.status_code}")
    return _object(response.json(), label=label)


def _post(request: HttpFixtureClient, path: str, *, label: str) -> dict[str, Any]:
    response = request("POST", path, None)
    if response.status_code != 200:
        raise RuntimeError(f"{label} returned {response.status_code}")
    return _object(response.json(), label=label)


def _fault_rows(*, compose_file: Path, fault_ids: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    script = """
import json, sqlite3, sys
ids=tuple(sys.argv[1:])
con=sqlite3.connect('/state/fault-control.sqlite3')
con.row_factory=sqlite3.Row
marks=','.join('?' for _ in ids)
query='select fault_id, target, mode, consumed_at from evaluation_faults '
query+=f'where fault_id in ({marks})'
rows=con.execute(
    query,
    ids,
).fetchall()
projected=[{
    'fault_id':r['fault_id'],
    'target':r['target'],
    'mode':r['mode'],
    'consumed':r['consumed_at'] is not None,
} for r in rows]
print(json.dumps(projected,sort_keys=True))
"""
    completed = subprocess.run(
        (
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "exec",
            "-T",
            "backend",
            "python",
            "-c",
            script,
            *fault_ids,
        ),
        cwd=compose_file.parent,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("Docker-domain fault-state query failed")
    value = json.loads(completed.stdout)
    if not isinstance(value, list) or len(value) != len(fault_ids):
        raise RuntimeError("Docker-domain fault-state query is incomplete")
    return {str(row["fault_id"]): dict(row) for row in value if isinstance(row, Mapping)}


def collect_summary(
    *,
    request: HttpFixtureClient,
    compose_file: Path,
    deployment_ref: str,
    graph_version_ref: str,
    cases: tuple[tuple[str, str, str], ...],
    browser_observations: Mapping[str, Any],
    d012_identity: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Join UI observations, exact fault rows, signed audit, and zero economics."""
    if tuple(item[0] for item in cases) != EXPECTED_MODES:
        raise RuntimeError("collector cases are not the exact expected fault modes")
    health = _get(request, "/health", label="restored health")
    if any(health.get(key) != value for key, value in d012_identity.items()):
        raise RuntimeError("D-012 serving identity is not restored exactly")
    observations = browser_observations.get("observations")
    if not isinstance(observations, list):
        raise RuntimeError("browser observations are missing")
    observation_by_mode = {row.get("mode"): row for row in observations if isinstance(row, Mapping)}
    fault_rows = _fault_rows(
        compose_file=compose_file,
        fault_ids=tuple(item[2] for item in cases),
    )
    network: list[dict[str, object]] = []
    summary_cases: list[dict[str, Any]] = []
    for mode, run_id, fault_id in cases:
        run = _get(request, f"/v1/runs/{run_id}", label=f"{mode} run")
        timeline = _get(request, f"/v1/runs/{run_id}/timeline", label=f"{mode} timeline")
        evidence = _get(request, f"/v1/runs/{run_id}/evidence", label=f"{mode} evidence")
        verification = _post(
            request, f"/v1/runs/{run_id}/verify-chain", label=f"{mode} verification"
        )
        for method, suffix in (
            ("GET", ""),
            ("GET", "/timeline"),
            ("GET", "/evidence"),
            ("POST", "/verify-chain"),
        ):
            network.append(
                {
                    "method": method,
                    "path": f"/v1/runs/{run_id}{suffix}",
                    "status": 200,
                    "sanitized": True,
                }
            )
        entries = timeline.get("entries")
        if not isinstance(entries, list) or not all(isinstance(row, Mapping) for row in entries):
            raise RuntimeError(f"{mode} timeline entries are invalid")
        audits = evidence.get("audits")
        if not isinstance(audits, list) or not all(isinstance(row, Mapping) for row in audits):
            raise RuntimeError(f"{mode} evidence audits are invalid")
        evidence_summary = _object(evidence.get("summary"), label=f"{mode} economics")
        provider_request_ids = []
        cost_event_ids = []
        for row in audits:
            metadata = row.get("execution_metadata")
            if isinstance(metadata, Mapping) and metadata.get("provider_request_id") is not None:
                provider_request_ids.append(metadata["provider_request_id"])
            if row.get("cost_event_id") is not None:
                cost_event_ids.append(row["cost_event_id"])
        fault = fault_rows.get(fault_id)
        if (
            fault is None
            or fault.get("target") != "provider"
            or fault.get("mode") != mode
            or fault.get("consumed") is not True
        ):
            raise RuntimeError(f"{mode} exact one-shot fault was not consumed")
        observation = observation_by_mode.get(mode)
        if (
            not isinstance(observation, Mapping)
            or observation.get("run_id") != run_id
            or observation.get("configured") is not True
            or observation.get("failed_visible") is not True
            or observation.get("refresh_restored") is not True
        ):
            raise RuntimeError(f"{mode} browser refresh evidence is incomplete")
        failure = run.get("failure_state")
        failure = failure if isinstance(failure, Mapping) else {}
        summary_cases.append(
            {
                "mode": mode,
                "fault_id": fault_id,
                "fault_consumed": True,
                "run_id": run_id,
                "status": run.get("status"),
                "failure_reason": failure.get("reason"),
                "timeline_node_ids": [row.get("node_id") for row in entries],
                "timeline_statuses": [row.get("status") for row in entries],
                "audit_verified": verification.get("verified"),
                "signature_verified": verification.get("signature_verified"),
                "audit_record_count": verification.get("record_count"),
                "unsigned_record_count": verification.get("unsigned_record_count"),
                "provider_request_ids": provider_request_ids,
                "cost_event_ids": cost_event_ids,
                "priced_call_count": evidence_summary.get("priced_call_count"),
                "cost_event_count": evidence_summary.get("cost_event_count"),
                "total_cost_usd": evidence_summary.get("total_cost_usd"),
                "cost_identity_state": evidence_summary.get("cost_identity_state"),
                "reconciliation_state": evidence_summary.get("reconciliation_state"),
                "refresh": {
                    "before_run_id": run_id,
                    "restored_run_id": run_id,
                    "restored_status": run.get("status"),
                },
            }
        )
    d012 = dict(d012_identity)
    summary = {
        "schema_version": 1,
        "deployment_ref": deployment_ref,
        "graph_version_ref": graph_version_ref,
        "provider_calls_performed": 0,
        "cases": summary_cases,
        "d012_restore": {"before": d012, "after": dict(d012), "exact": True},
    }
    validate_provider_fault_summary(
        summary,
        expected_deployment_ref=deployment_ref,
        expected_graph_version_ref=graph_version_ref,
    )
    return summary, {"schema_version": 1, "requests": network}


def _exclusive_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--deployment-ref", required=True)
    parser.add_argument("--graph-version-ref", required=True)
    parser.add_argument("--case", action="append", required=True)
    args = parser.parse_args()
    cases = []
    for encoded in args.case:
        parts = encoded.split(":")
        if len(parts) != 3:
            raise RuntimeError("case must be mode:run_id:fault_id")
        cases.append(tuple(parts))
    source_root = args.source_root.expanduser().resolve(strict=True)
    observations = json.loads(
        (source_root / "browser/browser-observations.json").read_text(encoding="utf-8")
    )
    request = HttpFixtureClient(
        base_url="http://127.0.0.1:8122",
        api_key=args.api_key_file.read_text(encoding="utf-8").strip(),
        tenant_id="evaluation-studio-v1",
    )
    d012 = {
        "status": "ok",
        "campaign_id": "evaluation-studio-v1",
        "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
        "deployment_version": 1,
        "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
    }
    summary, network = collect_summary(
        request=request,
        compose_file=args.compose_file.expanduser().resolve(strict=True),
        deployment_ref=args.deployment_ref,
        graph_version_ref=args.graph_version_ref,
        cases=tuple(cases),
        browser_observations=observations,
        d012_identity=d012,
    )
    _exclusive_json(source_root / "runtime/summary.json", summary)
    _exclusive_json(source_root / "browser/network/summary.json", network)
    print(json.dumps({"collected": True, "runs": len(cases), "provider_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
