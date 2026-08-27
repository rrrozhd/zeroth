"""Validate and seal the current Playwright loop/code evidence roots."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .evidence import AcceptanceCriterion, EvidenceStore

EXPECTED_CRITERIA = {
    "loops-code": {
        "code.content-identity",
        "code.execution-timeout",
        "code.inline-published",
        "code.malformed-output",
        "code.manifest-missing",
        "code.read-only",
        "loops.both-demos",
        "loops.dedicated-node",
        "loops.max-retries-visible",
        "loops.repeat-done-limit",
        "runs.failure-display",
        "runs.inline-success",
        "studio.preflight-error-focus",
    },
    "quality-loop": {
        "fields.run-payload-json",
        "loops.malformed-input",
        "loops.repeat-done",
        "loops.three-repetitions",
        "manifests.execution",
        "runs.refresh-restoration",
    },
    "incident-loop": {
        "fields.run-payload-json",
        "loops.limit-route",
        "loops.malformed-input",
        "loops.max-retries",
        "loops.repeat-done",
        "loops.three-repetitions",
        "manifests.execution",
        "runs.refresh-restoration",
    },
    "inline-code": {
        "code.content-identity-live",
        "code.inline-studio-execution",
        "code.zero-provider-cost",
        "runs.inline-refresh-restoration",
    },
}


def _load_results(root: Path, *, label: str) -> dict[str, object]:
    value = json.loads((root / "results.json").read_text())
    if not isinstance(value, dict) or value.get("completed") is not True:
        raise RuntimeError(f"Playwright root did not complete: {label}")
    criteria = value.get("criteria")
    if not isinstance(criteria, Sequence):
        raise RuntimeError(f"Playwright root has no criteria: {label}")
    statuses = {
        str(row.get("criterion_id")): row.get("status")
        for row in criteria
        if isinstance(row, Mapping)
    }
    missing = EXPECTED_CRITERIA[label] - set(statuses)
    failed = {criterion for criterion, status in statuses.items() if status != "pass"}
    if missing or failed:
        raise RuntimeError(
            f"Playwright criteria mismatch for {label}: missing={missing}, failed={failed}"
        )
    return value


def build_checkpoint(
    *,
    roots: Mapping[str, Path],
    destination: Path,
) -> Path:
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    resolved = {
        label: root.expanduser().resolve(strict=True) for label, root in roots.items()
    }
    results = {
        label: _load_results(root, label=label) for label, root in resolved.items()
    }

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "playwright-loops-and-code",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": "evaluation-studio-v1",
            "provider_calls_performed": 0,
            "source_roots": {label: str(root) for label, root in resolved.items()},
            "criteria_count": sum(
                len(value["criteria"]) for value in results.values()
            ),
        }
    )

    evidence_paths: list[str] = []
    for label, root in resolved.items():
        results_relative = Path("playwright-report") / f"{label}-results.json"
        store.ingest_artifact(root / "results.json", results_relative)
        evidence_paths.append(results_relative.as_posix())
        report_relative = Path("playwright-report") / f"{label}-index.html"
        store.ingest_artifact(root / "html-report" / "index.html", report_relative)
        evidence_paths.append(report_relative.as_posix())

        artifacts = results[label].get("artifacts")
        if not isinstance(artifacts, Sequence):
            raise RuntimeError(f"Playwright root has no artifacts: {label}")
        for row in artifacts:
            if not isinstance(row, Mapping):
                continue
            source_name = row.get("source")
            destination_name = row.get("destination")
            if not isinstance(source_name, str) or not isinstance(destination_name, str):
                raise RuntimeError(f"invalid indexed artifact: {label}")
            if destination_name == "playwright-report/index.html":
                continue
            original = Path(destination_name)
            if len(original.parts) < 2:
                raise RuntimeError(f"invalid artifact destination: {destination_name}")
            relative = Path(original.parts[0]) / f"{label}-{original.name}"
            store.ingest_artifact(root / source_name, relative)
            evidence_paths.append(relative.as_posix())

    event_id = store.append_event(
        "campaign.playwright.loops_code_verified",
        {
            "result": "pass",
            "provider_call_count": 0,
            "source_root_count": len(resolved),
            "proof_paths": evidence_paths,
        },
    )
    evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    acceptance = tuple(
        AcceptanceCriterion(f"PLAYWRIGHT-{criterion.upper().replace('.', '-')}", "pass", evidence)
        for criterion in sorted(set().union(*EXPECTED_CRITERIA.values()))
    )
    store.finalize_bundle(
        acceptance=acceptance,
        report_markdown=(
            "# Playwright loops and code checkpoint\n\n"
            "The provider-independent UI gate passed for both dedicated Loop demos, "
            "three happy-path repetitions per demo, malformed payload rejection, the "
            "incident Limit route at two retries, published inline code, successful and "
            "failed run inspection, and missing-manifest preflight rejection. The bundle "
            "contains sanitized runtime joins, screenshots, videos, and HTML reports.\n"
        ),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loops-code-root", type=Path, required=True)
    parser.add_argument("--quality-loop-root", type=Path, required=True)
    parser.add_argument("--incident-loop-root", type=Path, required=True)
    parser.add_argument("--inline-code-root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    root = build_checkpoint(
        roots={
            "loops-code": args.loops_code_root,
            "quality-loop": args.quality_loop_root,
            "incident-loop": args.incident_loop_root,
            "inline-code": args.inline_code_root,
        },
        destination=args.destination,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
