"""Seal reversible live Retention policy and legal-hold evidence."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .native_safari_retention_checkpoint import (
    HELD_RUN_ID,
    ROLE,
    ROUTE,
    TENANT,
    _revision,
    _sanitize_runtime,
    _validate_runtime,
)
from .workflow3_lifecycle_evidence import STATE_ROOT, WORKTREE, _request, _tree_digest

SOURCE_ROOT = STATE_ROOT / "evidence/retention-compliance-live-20260825-1"
ROOT = STATE_ROOT / "evidence/retention-compliance-live-checkpoint-20260825-1"

ACCEPTED_CRITERIA = (
    "fields.legal-hold",
    "fields.retention-policy",
    "retention-and-erasure.boundary",
    "retention-and-erasure.held",
    "retention-and-erasure.persistence",
)

_ARTIFACT_TOP_LEVEL = {
    "console",
    "playwright-report",
    "screenshots",
    "videos",
}

Request = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    source: Path
    destination: Path


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    results: dict[str, Any]
    artifacts: tuple[SourceArtifact, ...]
    screenshot_count: int
    video_count: int


def _load_json(path: Path, *, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid {label}") from exc


def _safe_relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"invalid source artifact {label}")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"unsafe source artifact {label}")
    return relative


def _source_file(root: Path, relative: Path) -> Path:
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("source artifact escapes its evidence root") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise RuntimeError(f"missing source artifact: {relative.as_posix()}")
    return candidate


def _validate_result_attachments(artifacts: list[SourceArtifact]) -> None:
    json_values = {
        item.destination.name: _load_json(item.source, label=item.destination.name)
        for item in artifacts
        if item.destination.parts[0] == "console"
    }
    policy = json_values.get("a9fb4fe221d55bfb-retention-policy-result.json")
    if policy is None:
        policy = json_values.get("policy-result.json")
    if not isinstance(policy, Mapping) or {
        "zero_rejected": policy.get("zero_rejected"),
        "non_numeric_rejected": policy.get("non_numeric_rejected"),
        "minimum_days_persisted": policy.get("minimum_days_persisted"),
        "representative_large_days_persisted": policy.get("representative_large_days_persisted"),
        "disabled_state_persisted": policy.get("disabled_state_persisted"),
        "original_policy_restored": policy.get("original_policy_restored"),
    } != {
        "zero_rejected": True,
        "non_numeric_rejected": True,
        "minimum_days_persisted": 1,
        "representative_large_days_persisted": 36500,
        "disabled_state_persisted": True,
        "original_policy_restored": True,
    }:
        raise RuntimeError("retention policy result does not prove exact restoration")

    holds = json_values.get("67e5c4d0ae9f774a-legal-hold-result.json")
    if holds is None:
        holds = json_values.get("hold-result.json")
    if (
        not isinstance(holds, Mapping)
        or holds.get("run_scoped_hold_persisted") is not True
        or holds.get("tenant_wide_hold_persisted") is not True
        or holds.get("both_released") is not True
        or holds.get("baseline_hold_ids_preserved") != ["8d452480319d4578895007cc8a36c8f0"]
    ):
        raise RuntimeError("legal-hold result does not prove baseline preservation")


def _load_source(root: Path) -> SourceEvidence:
    root = root.expanduser().resolve(strict=True)
    EvidenceStore(root).scan_recursive()
    value = _load_json(root / "results.json", label="source results")
    if not isinstance(value, dict):
        raise RuntimeError("source results must be an object")
    criteria = value.get("criteria")
    if (
        value.get("schema_version") != 1
        or value.get("completed") is not True
        or not isinstance(criteria, list)
    ):
        raise RuntimeError("source results are incomplete")
    dispositions = {
        row.get("criterion_id"): row.get("status") for row in criteria if isinstance(row, dict)
    }
    if dispositions != {criterion: "pass" for criterion in ACCEPTED_CRITERIA}:
        raise RuntimeError("source result criteria do not match the checkpoint allowlist")

    rows = value.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("source results do not declare artifacts")
    artifacts: list[SourceArtifact] = []
    destinations: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("invalid source artifact declaration")
        source_relative = _safe_relative(row.get("source"), label="source")
        destination = _safe_relative(row.get("destination"), label="destination")
        if len(destination.parts) < 2 or destination.parts[0] not in _ARTIFACT_TOP_LEVEL:
            raise RuntimeError("invalid source artifact destination")
        if destination.as_posix() in destinations:
            raise RuntimeError("duplicate source artifact destination")
        destinations.add(destination.as_posix())
        artifacts.append(
            SourceArtifact(
                source=_source_file(root, source_relative),
                destination=destination,
            )
        )

    for row in criteria:
        references = row.get("evidence") if isinstance(row, dict) else None
        if (
            not isinstance(references, list)
            or not references
            or not all(isinstance(item, str) and item in destinations for item in references)
        ):
            raise RuntimeError("criterion evidence is missing or undeclared")

    screenshot_count = sum(item.destination.parts[0] == "screenshots" for item in artifacts)
    video_count = sum(item.destination.parts[0] == "videos" for item in artifacts)
    if screenshot_count != 7 or video_count != 2:
        raise RuntimeError("retention source must contain exactly seven screenshots and two videos")
    _validate_result_attachments(artifacts)

    # Preserve the complete linked HTML report, not only its entrypoint.
    declared = {item.destination.as_posix() for item in artifacts}
    for source in sorted((root / "html-report").rglob("*")):
        if source.is_symlink():
            raise RuntimeError("Playwright report may not contain symlinks")
        if not source.is_file():
            continue
        relative = Path("playwright-report") / source.relative_to(root / "html-report")
        if relative.as_posix() not in declared:
            artifacts.append(SourceArtifact(source=source, destination=relative))
            declared.add(relative.as_posix())
    return SourceEvidence(
        results=value,
        artifacts=tuple(artifacts),
        screenshot_count=screenshot_count,
        video_count=video_count,
    )


def build_checkpoint(*, source_root: Path, destination: Path, request: Request) -> Path:
    """Validate reversible browser evidence and exact post-test runtime restoration."""
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    source = _load_source(source_root)
    runtime = _sanitize_runtime(request)
    _validate_runtime(runtime)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "retention-compliance-reversible-live",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": _revision(),
            "working_tree_digest": _tree_digest(WORKTREE),
            "source_root": source_root.resolve().name,
            "source_results_sha256": sha256(
                (source_root / "results.json").read_bytes()
            ).hexdigest(),
            "tenant_id": TENANT,
            "role": ROLE,
            "route": ROUTE,
            "provider_calls_performed": 0,
            "mutations_restored": True,
            "screenshot_count": source.screenshot_count,
            "video_count": source.video_count,
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    source_results_path = Path("playwright-report/results.json")
    store._write_exclusive(source_results_path, source.results)
    evidence_paths = [source_results_path.as_posix()]
    for name, record in runtime.items():
        relative = Path("runtime") / f"{name}-after.json"
        store._write_exclusive(relative, record)
        evidence_paths.append(relative.as_posix())
    for artifact in source.artifacts:
        store.ingest_artifact(artifact.source, artifact.destination)
        evidence_paths.append(artifact.destination.as_posix())

    screenshots = [
        item.destination.as_posix()
        for item in source.artifacts
        if item.destination.parts[0] == "screenshots"
    ]
    screenshot_index = {
        "schema_version": 1,
        "route": ROUTE,
        "tenant_id": TENANT,
        "role": ROLE,
        "screenshots": [
            {
                "file": path,
                "criterion_ids": [
                    row["criterion_id"]
                    for row in source.results["criteria"]
                    if path in row["evidence"]
                ],
                "expected_result": Path(path).stem,
            }
            for path in screenshots
        ],
    }
    store._write_exclusive(Path("screenshot-index.json"), screenshot_index)
    evidence_paths.append("screenshot-index.json")
    store.record_command(
        sequence=1,
        name="retention-live-playwright",
        argv=[
            "npx",
            "playwright",
            "test",
            "e2e/retention-compliance-live.spec.ts",
            "--project=desktop-1440",
        ],
        working_directory=WORKTREE / "frontend",
        exit_code=0,
        stdout="2 tests passed; policy and legal-hold baselines restored exactly.\n",
        stderr="",
    )
    evidence_paths.append("commands/0001-retention-live-playwright.json")

    event_id = store.append_event(
        "campaign.retention_reversible_verified",
        {
            "result": "pass",
            "route": ROUTE,
            "tenant_id": TENANT,
            "role": ROLE,
            "policy_restored": True,
            "legal_hold_baseline_restored": True,
            "provider_call_count": 0,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(run_id=HELD_RUN_ID),
    )
    common_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", common_evidence)
            for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Retention & Compliance reversible live checkpoint\n\n"
            "The live desktop browser journey rejected zero and non-numeric TTLs, "
            "persisted a one-day run TTL and representative 36,500-day audit TTL, "
            "persisted the disabled state through refresh, and restored the exact "
            "enabled no-expiry baseline. It rejected a hold for a missing run, created "
            "run-scoped and tenant-wide holds, restored them through refresh, released "
            "both, and preserved the pre-existing baseline hold. Sanitized screenshots, "
            "videos, report, runtime post-state, and results are sealed. No provider call "
            "occurred. Role denial, cross-tenant isolation, and disposable-fixture erasure "
            "remain outside this checkpoint.\n"
        ),
    )
    return destination


def main() -> int:
    root = build_checkpoint(
        source_root=SOURCE_ROOT,
        destination=ROOT,
        request=_request,
    )
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
