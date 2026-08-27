"""Fail-closed sealer for the provider-independent resilient-HTTP UI journey."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .control_plane import dirty_tree_hash
from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore

EXPECTED_CRITERIA = {
    "resilient-http.field-contract",
    "resilient-http.retry-success",
    "resilient-http.timeout-exhaustion",
    "resilient-http.circuit-open",
    "resilient-http.recovery",
    "resilient-http.sanitized-signed-audit",
    "resilient-http.zero-provider-economics",
}
EXPECTED_D012 = {
    "deployment_ref": "provider-free-child-approval-d012-20260826-2-parent",
    "deployment_version": 1,
    "graph_version_ref": "0179d403-2863-45f3-9556-58052a992da8@1",
}


def _object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label}")
    return value


def validate_resilient_http_summary(summary: Mapping[str, Any]) -> dict[str, object]:
    health = summary.get("health")
    runs = summary.get("runs")
    audits = summary.get("audits")
    scenario = summary.get("scenario_events")
    if (
        summary.get("schema_version") != 1
        or not isinstance(health, Mapping)
        or health.get("status") != "ok"
        or not isinstance(runs, list)
        or not isinstance(audits, list)
        or not isinstance(scenario, Mapping)
    ):
        raise RuntimeError("resilient-HTTP summary shape is invalid")
    statuses = [row.get("status") for row in runs if isinstance(row, Mapping)]
    if statuses != ["succeeded", "failed", "failed", "failed", "succeeded"]:
        raise RuntimeError("resilient-HTTP run outcomes are not exact")
    run_ids = [row.get("run_id") for row in runs if isinstance(row, Mapping)]
    if len(set(run_ids)) != 5 or any(not isinstance(value, str) or not value for value in run_ids):
        raise RuntimeError("resilient-HTTP run identities are invalid")
    if (
        summary.get("audit_count") != len(audits)
        or not audits
        or any(
            not isinstance(row, Mapping)
            or row.get("record_signature_present") is not True
            or row.get("cost_event_id") is not None
            for row in audits
        )
    ):
        raise RuntimeError("resilient-HTTP signed audit reconciliation failed")
    http_rows = [
        row
        for row in audits
        if isinstance(row, Mapping)
        and isinstance(row.get("execution_metadata"), Mapping)
        and row["execution_metadata"].get("node_kind") == "http_request"
    ]
    shapes = [
        (
            row.get("node_id"),
            row.get("status"),
            row["execution_metadata"].get("reason_code"),
            row["execution_metadata"].get("retry_count"),
            row["execution_metadata"].get("upstream_status_code"),
        )
        for row in http_rows
    ]
    required = {
        ("http-retry", "completed", None, 2, 200),
        ("http-timeout", "failed", "http_retry_exhausted_error", 2, None),
        ("http-circuit", "failed", "circuit_open_error", 0, None),
        ("http-circuit", "completed", None, 0, 200),
    }
    if not required.issubset(set(shapes)):
        raise RuntimeError("resilient-HTTP node outcomes are incomplete")
    for row in http_rows:
        metadata = row["execution_metadata"]
        digest = metadata.get("target_url_sha256")
        if not isinstance(digest, str) or len(digest) != 64 or "127.0.0.1" in json.dumps(metadata):
            raise RuntimeError("resilient-HTTP audit metadata is not sanitized")
    if (
        summary.get("provider_call_count") != 0
        or summary.get("cost_event_ids") != []
        or summary.get("total_cost_usd") != 0
    ):
        raise RuntimeError("provider-free economics are not exactly zero")
    events = scenario.get("events")
    if not isinstance(events, list) or scenario.get("recovered") is not True:
        raise RuntimeError("scenario recovery evidence is invalid")
    retry_statuses = [
        row.get("status_code")
        for row in events
        if isinstance(row, Mapping) and row.get("scenario") == "retry-then-success"
    ]
    if retry_statuses != [503, 503, 200] or not any(
        isinstance(row, Mapping)
        and row.get("scenario") == "circuit"
        and row.get("status_code") == 200
        for row in events
    ):
        raise RuntimeError("scenario retry/recovery sequence is invalid")
    return {
        "run_ids": run_ids,
        "audit_ids": [str(row["audit_id"]) for row in audits],
        "deployment_ref": str(health["deployment_ref"]),
        "graph_version_ref": str(health["graph_version_ref"]),
        "priced_call_count": 0,
        "total_cost_usd": 0.0,
    }


def _load_browser(browser_root: Path) -> tuple[dict[str, Any], dict[str, Path], tuple[Path, ...]]:
    EvidenceStore(browser_root).scan_recursive()
    results = _object(browser_root / "results.json", "browser results")
    criteria = results.get("criteria")
    if results.get("completed") is not True or not isinstance(criteria, list):
        raise RuntimeError("browser run did not complete")
    dispositions = {
        row.get("criterion_id"): row.get("status") for row in criteria if isinstance(row, Mapping)
    }
    if len(criteria) != 7 or dispositions != {item: "pass" for item in EXPECTED_CRITERIA}:
        raise RuntimeError("browser criteria do not match the exact allowlist")
    artifacts: dict[str, Path] = {}
    for row in results.get("artifacts", []):
        if not isinstance(row, Mapping):
            raise RuntimeError("invalid browser artifact")
        source = Path(str(row.get("source")))
        destination = Path(str(row.get("destination")))
        if (
            source.is_absolute()
            or destination.is_absolute()
            or ".." in source.parts
            or ".." in destination.parts
        ):
            raise RuntimeError("unsafe browser artifact path")
        candidate = (browser_root / source).resolve(strict=True)
        candidate.relative_to(browser_root)
        if destination.as_posix() in artifacts:
            raise RuntimeError("duplicate browser artifact destination")
        artifacts[destination.as_posix()] = candidate
    screenshots = [key for key in artifacts if key.startswith("screenshots/")]
    videos = [key for key in artifacts if key.startswith("videos/")]
    projects = ("desktop-1440", "webkit-1440")
    required_kinds = (
        "sanitized-network.json",
        "sanitized-console.json",
        "response-identities.json",
        "resilient-http-summary.json",
    )
    if (
        len(screenshots) != 12
        or len(videos) != 2
        or any(
            len([key for key in artifacts if key.endswith(name)]) != 2 for name in required_kinds
        )
    ):
        raise RuntimeError("browser evidence inventory is incomplete")
    for project in projects:
        qualified = [key for key in artifacts if Path(key).name.startswith(f"{project}-")]
        if (
            len([key for key in qualified if key.startswith("screenshots/")]) != 6
            or len([key for key in qualified if key.startswith("videos/")]) != 1
            or any(
                len([key for key in qualified if key.endswith(name)]) != 1
                for name in required_kinds
            )
        ):
            raise RuntimeError(f"{project} browser evidence inventory is incomplete")
    declared = set(artifacts) - {"playwright-report/index.html"}
    if any(
        set(row.get("evidence", [])) != declared for row in criteria if isinstance(row, Mapping)
    ):
        raise RuntimeError("criterion evidence references are incomplete")
    summaries = [
        _object(source, f"{project} resilient-HTTP summary")
        for project in projects
        for key, source in artifacts.items()
        if Path(key).name.startswith(f"{project}-") and key.endswith("resilient-http-summary.json")
    ]
    identities = [validate_resilient_http_summary(summary) for summary in summaries]
    if len(summaries) != 2 or len({item["deployment_ref"] for item in identities}) != 1:
        raise RuntimeError("cross-browser resilient-HTTP identities drifted")
    report = browser_root / "html-report"
    report_files = tuple(path for path in sorted(report.rglob("*")) if path.is_file())
    if not (report / "index.html").is_file() or not any(
        path.parent != report for path in report_files
    ):
        raise RuntimeError("complete Playwright HTML report is missing")
    return summaries[0], artifacts, report_files


def build_checkpoint(*, source_root: Path, destination: Path, repository_root: Path) -> Path:
    source_root = source_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    repository_root = repository_root.expanduser().resolve(strict=True)
    if destination.exists():
        raise FileExistsError(destination)
    summary, artifacts, report_files = _load_browser(source_root / "browser")
    identities = validate_resilient_http_summary(summary)
    restore = _object(source_root / "runtime" / "d012-restore.json", "D-012 restore")
    if (
        restore.get("exact") is not True
        or restore.get("before") != EXPECTED_D012
        or restore.get("after") != EXPECTED_D012
    ):
        raise RuntimeError("D-012 restoration is not exact")
    fixture = _object(source_root / "runtime" / "fixture.json", "fixture")
    if fixture.get("provider_calls_performed") != 0:
        raise RuntimeError("fixture performed provider calls")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "resilient-http-accepted-20260826-1",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": "evaluation-studio-v1",
            "revision": revision,
            "diff_sha256": dirty_tree_hash(repository_root).removeprefix("sha256:"),
            "source_root": str(source_root),
            "served_identity": summary["health"],
            "execution_identities": identities,
            "provider_calls_performed": 0,
            "total_cost_usd": 0.0,
            "d012_restore": restore,
        }
    )
    evidence: list[str] = []
    for relative, source in sorted(artifacts.items()):
        if relative == "playwright-report/index.html":
            continue
        store.ingest_artifact(source, relative)
        evidence.append(relative)
    report_root = source_root / "browser" / "html-report"
    for source in report_files:
        relative = Path("playwright-report") / source.relative_to(report_root)
        store.ingest_artifact(source, relative)
        evidence.append(relative.as_posix())
    source_artifacts = {
        "browser/results.json": "playwright-report/results.json",
        "runtime/fixture.json": "reconciliation/fixture.json",
        "runtime/d012-restore.json": "reconciliation/d012-restore.json",
        "commands/playwright.stdout.txt": "handoff/commands/playwright.stdout.txt",
        "commands/playwright.stderr.txt": "handoff/commands/playwright.stderr.txt",
        "commands/playwright.exit.txt": "handoff/commands/playwright.exit.txt",
    }
    for source_relative, destination_relative in source_artifacts.items():
        store.ingest_artifact(source_root / source_relative, destination_relative)
        evidence.append(destination_relative)
    event_id = store.append_event(
        "campaign.run.resilient_http_verified",
        {
            "result": "pass",
            "provider_call_count": 0,
            "total_cost_usd": 0.0,
            "audit_count": summary["audit_count"],
        },
        correlation=CorrelationIds(run_id=str(identities["run_ids"][-1])),
    )
    refs = tuple([*evidence, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=[AcceptanceCriterion(item, "pass", refs) for item in sorted(EXPECTED_CRITERIA)],
        report_markdown=(
            "# Resilient HTTP UI checkpoint\n\n"
            "The real Studio UI configured a private GET node, proved retry then success, timeout "
            "exhaustion, circuit refusal, reset recovery, signed sanitized audit records, "
            "and exactly zero provider calls and cost. The controlled peer remained on "
            "backend-container loopback. "
            "D-012 was restored exactly after the journey.\n"
        ),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    args = parser.parse_args()
    root = build_checkpoint(
        source_root=args.source_root,
        destination=args.destination,
        repository_root=args.repository_root,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
