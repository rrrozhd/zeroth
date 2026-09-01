"""Candidate-bound acceptance harness for the headless economic debugger."""

from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx


def seed_contract() -> dict[str, Any]:
    """Return the deterministic economic evidence used by release acceptance."""
    executions = [
        {
            "execution_id": "release-success",
            "join_key": "run-success",
            "timestamp": "2026-08-02T12:00:00Z",
            "capability_id": "invoice-processing",
            "implementation_id": "invoice-processing:v1",
            "model_version": "gpt-5",
            "workflow_id": "invoice-processing",
            "workflow_version": "v1",
            "run_id": "run-success",
            "step_id": "extract",
            "attempt": 1,
            "subject_id": "release-subject-success",
            "dimensions": {"plan": "production"},
            "token_cost_usd": "0.10",
            "cost_measurement": "measured",
            "usage_measurement": "measured",
            "metadata": {"provider": "openai", "model": "gpt-5", "project_id": "proj-a"},
        },
        {
            "execution_id": "release-failure-1",
            "join_key": "run-failed",
            "timestamp": "2026-08-03T12:00:00Z",
            "capability_id": "invoice-processing",
            "implementation_id": "invoice-processing:v1",
            "model_version": "gpt-5",
            "workflow_id": "invoice-processing",
            "workflow_version": "v1",
            "run_id": "run-failed",
            "step_id": "extract",
            "attempt": 1,
            "subject_id": "release-subject-failed",
            "dimensions": {"plan": "free"},
            "token_cost_usd": "0.30",
            "cost_measurement": "measured",
            "usage_measurement": "measured",
            "metadata": {"provider": "openai", "model": "gpt-5", "project_id": "proj-a"},
        },
        {
            "execution_id": "release-failure-2",
            "join_key": "run-failed",
            "timestamp": "2026-08-03T12:00:01Z",
            "capability_id": "invoice-processing",
            "implementation_id": "invoice-processing:v1",
            "model_version": "gpt-5",
            "workflow_id": "invoice-processing",
            "workflow_version": "v1",
            "run_id": "run-failed",
            "step_id": "extract",
            "attempt": 2,
            "subject_id": "release-subject-failed",
            "dimensions": {"plan": "free"},
            "token_cost_usd": "0.10",
            "cost_measurement": "measured",
            "usage_measurement": "measured",
            "metadata": {"provider": "openai", "model": "gpt-5", "project_id": "proj-a"},
        },
    ]
    return {
        "outcome_definition": {
            "workflow_id": "invoice-processing",
            "workflow_version": "v1",
            "outcome_type": "approval",
            "operator": "equals",
            "target": True,
        },
        "executions": executions,
        "outcomes": [
            {
                "execution_id": "release-success",
                "join_key": "run-success",
                "capability_id": "invoice-processing",
                "implementation_id": "invoice-processing:v1",
                "outcome_type": "approval",
                "outcome_value": True,
                "occurred_at": "2026-08-02T12:00:02Z",
            },
            {
                "execution_id": "release-failure-2",
                "join_key": "run-failed",
                "capability_id": "invoice-processing",
                "implementation_id": "invoice-processing:v1",
                "outcome_type": "approval",
                "outcome_value": False,
                "occurred_at": "2026-08-03T12:00:02Z",
            },
        ],
        "provider_statement": {
            "statement_id": "release-openai-2026-08",
            "provider": "openai",
            "period_start": "2026-08-01T00:00:00Z",
            "period_end": "2026-09-01T00:00:00Z",
            "currency": "USD",
            "billed_total_usd": "0.50",
            "source_kind": "cost_api",
            "buckets": [
                {
                    "bucket_id": "proj-a-gpt-5",
                    "period_start": "2026-08-01T00:00:00Z",
                    "period_end": "2026-09-01T00:00:00Z",
                    "amount_usd": "0.50",
                    "model": "gpt-5",
                    "provider_dimensions": {"project_id": "proj-a"},
                }
            ],
        },
    }


def _post(client: httpx.Client, path: str, payload: dict[str, Any]) -> None:
    response = client.post(path, json=payload)
    if response.status_code not in {200, 201}:
        raise ValueError(f"seed request {path} failed: {response.status_code} {response.text}")


def seed_service(*, base_url: str, token: str, statement_output: Path) -> None:
    """Seed the installed plane through the public authenticated client contract."""
    from zeroth.econ.instrumentation import (
        ExecutionEvent,
        InstrumentationClient,
        OutcomeEvent,
    )

    contract = seed_contract()
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=30.0) as client:
        _post(client, "/debugger/outcome-definitions", contract["outcome_definition"])
    with InstrumentationClient.authenticated(
        base_url=base_url,
        bearer_token=token,
        timeout=30.0,
    ) as instrumentation:
        for execution in contract["executions"]:
            instrumentation.track_execution_confirmed(
                ExecutionEvent.model_validate(execution)
            )
        for outcome in contract["outcomes"]:
            instrumentation.track_outcome_confirmed(
                OutcomeEvent(
                    execution_id=outcome["execution_id"],
                    join_key=outcome["join_key"],
                    capability_id=outcome["capability_id"],
                    outcome_type=outcome["outcome_type"],
                    outcome_value=outcome["outcome_value"],
                    outcome_timestamp=outcome["occurred_at"],
                )
            )
    statement_output.write_text(
        json.dumps(contract["provider_statement"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _file_digest(path: Path) -> str:
    return "sha256:" + sha256(path.read_bytes()).hexdigest()


def _zero(value: object) -> bool:
    try:
        return Decimal(str(value)) == 0
    except (InvalidOperation, ValueError):
        return False


def build_report(
    *,
    candidate: dict[str, Any],
    artifact_digest: str,
    installed_distributions: dict[str, str],
    diagnostic: dict[str, Any],
    diagnostic_markdown: Path,
    reconciliation: dict[str, Any],
    reconciliation_markdown: Path,
) -> dict[str, Any]:
    """Validate the product proof and return its candidate-bound release report."""
    from gates.identity import identity_digest

    package = candidate.get("package", {})
    candidate_version = package.get("version")
    if installed_distributions.get("zeroth-core") != candidate_version:
        raise ValueError("installed zeroth-core version does not match the candidate")
    if artifact_digest not in set(package.get("artifacts", {}).values()):
        raise ValueError("installed artifact digest does not match the candidate")
    excluded = {
        name: "present" if name in installed_distributions else "absent"
        for name in ("zeroth-console", "zeroth-sdk")
    }
    if any(value != "absent" for value in excluded.values()):
        raise ValueError("headless install contains the UI or standalone SDK")
    if diagnostic.get("claim_scope") != "observed_economic_exposure" or diagnostic.get(
        "decision_state"
    ) != "economic_risk_observed":
        raise ValueError("diagnostic does not expose bounded economic risk")
    diagnostic_text = diagnostic_markdown.read_text(encoding="utf-8").lower()
    if "not which step caused" not in diagnostic_text or "does not prove savings" not in (
        diagnostic_text
    ):
        raise ValueError("diagnostic Markdown omits its causality or savings limit")
    if reconciliation.get("reconciliation_state") != "reconciled" or not _zero(
        reconciliation.get("unreconciled_billed_usd")
    ):
        raise ValueError("provider reconciliation did not close")
    if not _zero(reconciliation.get("outcome_unresolved_usd", 0)):
        raise ValueError("provider reconciliation retained unresolved outcomes")
    if "reconciled" not in reconciliation_markdown.read_text(encoding="utf-8").lower():
        raise ValueError("reconciliation Markdown does not state closure")
    return {
        "schema_version": 1,
        "status": "passed",
        "candidate_digest": identity_digest(candidate),
        "package": {
            "name": "zeroth-core",
            "version": candidate_version,
            "artifact_digest": artifact_digest,
        },
        "excluded_distributions": excluded,
        "diagnostic": {
            "claim_scope": diagnostic["claim_scope"],
            "decision_state": diagnostic["decision_state"],
            "measured_failure_exposure_usd": diagnostic.get(
                "measured_failure_exposure_usd"
            ),
            "markdown_sha256": _file_digest(diagnostic_markdown),
        },
        "reconciliation": {
            "reconciliation_state": reconciliation["reconciliation_state"],
            "billed_total_usd": reconciliation.get("billed_total_usd"),
            "allocated_billed_usd": reconciliation.get("allocated_billed_usd"),
            "unreconciled_billed_usd": reconciliation["unreconciled_billed_usd"],
            "outcome_unresolved_usd": reconciliation.get("outcome_unresolved_usd"),
            "markdown_sha256": _file_digest(reconciliation_markdown),
        },
    }


def _installed_distributions() -> dict[str, str]:
    installed: dict[str, str] = {}
    for name in ("zeroth-core", "zeroth-console", "zeroth-sdk"):
        try:
            installed[name] = version(name)
        except PackageNotFoundError:
            continue
    return installed


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _seed_command(args: argparse.Namespace) -> int:
    token = os.environ.get(args.token_env, "")
    if not token:
        raise ValueError(f"set {args.token_env} before seeding acceptance")
    seed_service(
        base_url=args.base_url,
        token=token,
        statement_output=Path(args.statement_output),
    )
    return 0


def _report_command(args: argparse.Namespace) -> int:
    report = build_report(
        candidate=_load_json(Path(args.identity)),
        artifact_digest=args.artifact_digest,
        installed_distributions=_installed_distributions(),
        diagnostic=_load_json(Path(args.diagnostic_json)),
        diagnostic_markdown=Path(args.diagnostic_markdown),
        reconciliation=_load_json(Path(args.reconciliation_json)),
        reconciliation_markdown=Path(args.reconciliation_markdown),
    )
    Path(args.output).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed = subparsers.add_parser("seed")
    seed.add_argument("--base-url", default="http://127.0.0.1:8001/v1")
    seed.add_argument("--token-env", default="ZEROTH_ECON_TOKEN")
    seed.add_argument("--statement-output", required=True)
    seed.set_defaults(func=_seed_command)
    report = subparsers.add_parser("report")
    report.add_argument("--identity", required=True)
    report.add_argument("--artifact-digest", required=True)
    report.add_argument("--diagnostic-json", required=True)
    report.add_argument("--diagnostic-markdown", required=True)
    report.add_argument("--reconciliation-json", required=True)
    report.add_argument("--reconciliation-markdown", required=True)
    report.add_argument("--output", required=True)
    report.set_defaults(func=_report_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"economic acceptance failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
