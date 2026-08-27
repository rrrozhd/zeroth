"""Coordinate and seal the provider-free D-012 child-approval checkpoint.

This module never restarts a service or captures a live database directly. The
operator supplies closed pre/post SQLite snapshots around one externally owned
backend restart. Provisioning and staging are explicit CLI phases so their
identities are durable before browser execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .child_approval_live import (
    BoundedChildApprovalUiRunner,
    ProviderFreeChildApprovalFixture,
    StagedChildApproval,
    provision_child_approval_fixture,
    recover_child_approval_fixture,
    stage_pending_child_approval,
    validate_child_approval_snapshots,
    validate_child_approval_summary,
)
from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .provider_free_composed import HttpFixtureClient

_FIXED_TITLE = "resolves exact child approve and reject after one coordinated restart"
_CRITERIA = (
    "subgraphs.child-approval-parent-visibility",
    "subgraphs.child-approval-restart-restoration",
    "subgraphs.child-approval-no-sibling-replay",
    "approvals.reason-ui",
    "audit.child-parent-signed-linkage",
    "economics.provider-free-zero-activity",
)


def _write_exclusive_json(destination: Path, value: Mapping[str, object]) -> Path:
    destination = destination.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    EvidenceStore(destination.parent).validate(value)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return destination


def _read_object(source: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(source.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"D-012 {label} is malformed") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"D-012 {label} must be a JSON object")
    EvidenceStore(source.parent).validate(value)
    return value


def _provider_free_envelope(value: Mapping[str, object], *, phase: str) -> None:
    if (
        value.get("schema_version") != 1
        or value.get("phase") != phase
        or value.get("sealed") is not False
        or value.get("provider_calls_performed") != 0
        or value.get("provider_economics_status") != "blocked"
    ):
        raise RuntimeError(f"D-012 {phase} manifest violates the provider-free boundary")


def write_provisioning_manifest(
    destination: Path, fixture: ProviderFreeChildApprovalFixture
) -> Path:
    if fixture.provider_calls_performed != 0 or fixture.provider_economics_status != "blocked":
        raise RuntimeError("D-012 fixture violates the provider-free boundary")
    return _write_exclusive_json(
        destination,
        {
            "schema_version": 1,
            "phase": "provisioning",
            "sealed": False,
            "provider_calls_performed": 0,
            "provider_economics_status": "blocked",
            "fixture": asdict(fixture),
        },
    )


def read_provisioning_manifest(source: Path) -> ProviderFreeChildApprovalFixture:
    value = _read_object(source, label="provisioning manifest")
    _provider_free_envelope(value, phase="provisioning")
    fixture = value.get("fixture")
    if not isinstance(fixture, dict):
        raise RuntimeError("D-012 provisioning manifest lacks its fixture")
    try:
        parsed = ProviderFreeChildApprovalFixture(**fixture)
    except TypeError as exc:
        raise RuntimeError("D-012 provisioning fixture is malformed") from exc
    if parsed.provider_calls_performed != 0 or parsed.provider_economics_status != "blocked":
        raise RuntimeError("D-012 provisioning fixture violates the provider-free boundary")
    return parsed


def write_staging_manifest(
    destination: Path,
    fixture: ProviderFreeChildApprovalFixture,
    staged: StagedChildApproval,
) -> Path:
    if fixture.provider_calls_performed != 0 or fixture.provider_economics_status != "blocked":
        raise RuntimeError("D-012 staging fixture violates the provider-free boundary")
    return _write_exclusive_json(
        destination,
        {
            "schema_version": 1,
            "phase": "staging",
            "sealed": False,
            "provider_calls_performed": 0,
            "provider_economics_status": "blocked",
            "fixture": asdict(fixture),
            "staged": asdict(staged),
        },
    )


def read_staging_manifest(
    source: Path,
) -> tuple[ProviderFreeChildApprovalFixture, StagedChildApproval]:
    value = _read_object(source, label="staging manifest")
    _provider_free_envelope(value, phase="staging")
    fixture = value.get("fixture")
    staged = value.get("staged")
    if not isinstance(fixture, dict) or not isinstance(staged, dict):
        raise RuntimeError("D-012 staging manifest lacks fixture or staged identity")
    try:
        parsed_fixture = ProviderFreeChildApprovalFixture(**fixture)
        parsed_staged = StagedChildApproval(**staged)
    except TypeError as exc:
        raise RuntimeError("D-012 staging identity is malformed") from exc
    if (
        parsed_fixture.provider_calls_performed != 0
        or parsed_fixture.provider_economics_status != "blocked"
    ):
        raise RuntimeError("D-012 staging fixture violates the provider-free boundary")
    return parsed_fixture, parsed_staged


def write_ui_result_manifest(destination: Path, result: Mapping[str, object]) -> Path:
    raw = result.get("raw_summary")
    validated = result.get("validated_summary")
    if not isinstance(raw, Mapping) or not isinstance(validated, Mapping):
        raise RuntimeError("D-012 UI result must preserve raw and validated summaries")
    if validate_child_approval_summary(raw) != dict(validated):
        raise RuntimeError("D-012 UI raw and validated summaries disagree")
    return _write_exclusive_json(
        destination,
        {
            "schema_version": 1,
            "phase": "ui-result",
            "sealed": False,
            "provider_calls_performed": 0,
            "provider_economics_status": "blocked",
            "raw_summary": dict(raw),
            "validated_summary": dict(validated),
        },
    )


def read_ui_result_manifest(source: Path) -> dict[str, object]:
    value = _read_object(source, label="UI result manifest")
    _provider_free_envelope(value, phase="ui-result")
    raw = value.get("raw_summary")
    validated = value.get("validated_summary")
    if not isinstance(raw, Mapping) or not isinstance(validated, Mapping):
        raise RuntimeError("D-012 UI result lost raw or validated summary")
    expected = validate_child_approval_summary(raw)
    if expected != dict(validated):
        raise RuntimeError("D-012 UI result summaries do not reconcile")
    return {**expected, "raw_summary": dict(raw), "validated_summary": dict(validated)}


def _browser_artifacts(browser_root: Path) -> tuple[dict[str, object], list[tuple[Path, Path]]]:
    browser_root = browser_root.expanduser().resolve(strict=True)
    index = _read_object(browser_root / "results.json", label="browser evidence index")
    if (
        index.get("schema_version") != 1
        or index.get("completed") is not True
        or index.get("fixed_title") != _FIXED_TITLE
    ):
        raise RuntimeError("D-012 browser evidence did not complete the fixed test")
    raw_artifacts = index.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise RuntimeError("D-012 browser evidence index lacks artifacts")
    artifacts: list[tuple[Path, Path]] = []
    destinations: set[str] = set()
    counts = {
        "screenshots": 0,
        "videos": 0,
        "network": 0,
        "console": 0,
        "playwright-report": 0,
    }
    for item in raw_artifacts:
        if not isinstance(item, Mapping):
            raise RuntimeError("D-012 browser artifact row is malformed")
        source_text = item.get("source")
        destination_text = item.get("destination")
        if not isinstance(source_text, str) or not isinstance(destination_text, str):
            raise RuntimeError("D-012 browser artifact lacks a source or destination")
        source_relative = Path(source_text)
        destination = Path(destination_text)
        if (
            source_relative.is_absolute()
            or destination.is_absolute()
            or ".." in source_relative.parts
            or ".." in destination.parts
            or len(destination.parts) < 2
            or destination.parts[0] not in counts
            or destination_text in destinations
        ):
            raise RuntimeError("D-012 browser artifact path is unsafe or duplicated")
        source = (browser_root / source_relative).resolve(strict=True)
        if source.is_symlink() or not source.is_file() or browser_root not in source.parents:
            raise RuntimeError("D-012 browser artifact escaped its evidence root")
        counts[destination.parts[0]] += 1
        destinations.add(destination_text)
        artifacts.append((source, destination))
    if (
        counts["screenshots"] < 4
        or counts["videos"] < 1
        or counts["network"] < 1
        or counts["playwright-report"] < 1
    ):
        raise RuntimeError("D-012 browser evidence lacks screenshots, video, network, or results")
    return index, artifacts


def _snapshot_attestation(source: Path) -> dict[str, object]:
    """Describe one independently validated closed snapshot without copying tenant-wide data."""
    source = source.expanduser().resolve(strict=True)
    payload = source.read_bytes()
    return {
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "closed_snapshot_validated": True,
        "raw_snapshot_in_sealed_bundle": False,
    }


def seal_child_approval_checkpoint(
    *,
    destination: Path,
    tenant_id: str,
    provisioning_manifest: Path,
    staging_manifest: Path,
    ui_result_manifest: Path,
    before_snapshot: Path,
    after_snapshot: Path,
    browser_root: Path,
) -> Path:
    """Validate independent snapshots/UI records, then seal one append-only bundle."""
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    if not tenant_id:
        raise ValueError("D-012 tenant identity is required")
    fixture = read_provisioning_manifest(provisioning_manifest)
    staged_fixture, staged = read_staging_manifest(staging_manifest)
    if staged_fixture != fixture:
        raise RuntimeError("D-012 provisioning and staging fixtures disagree")
    ui_result = read_ui_result_manifest(ui_result_manifest)
    raw_summary = ui_result["raw_summary"]
    validated_summary = ui_result["validated_summary"]
    assert isinstance(raw_summary, Mapping)
    assert isinstance(validated_summary, Mapping)
    snapshot_validation = validate_child_approval_snapshots(
        before_snapshot,
        after_snapshot,
        tenant_id=tenant_id,
        fixture=fixture,
        staged=staged,
        summary=raw_summary,
    )
    browser_index, browser_artifacts = _browser_artifacts(browser_root)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "d012-child-approval-parent-continuation",
            "created_at": datetime.now(UTC).isoformat(),
            "tenant_id": tenant_id,
            "fixture_id": fixture.fixture_id,
            "parent_deployment_ref": fixture.parent_deployment_ref,
            "parent_graph_version_ref": fixture.parent_graph_version_ref,
            "staged_parent_run_id": staged.parent_run_id,
            "provider_calls_performed": 0,
            "provider_economics_status": "blocked",
            "provider_economics_claimed": False,
        }
    )
    paths = [
        "manifest.json",
        "database-snapshots/closed-snapshot-attestations.json",
        "handoff/provisioning-manifest.json",
        "handoff/staging-manifest.json",
        "runtime/raw-playwright-summary.json",
        "runtime/validated-playwright-summary.json",
        "runtime/snapshot-validation.json",
        "playwright-report/evidence-index.json",
    ]
    store._write_exclusive(
        Path("database-snapshots/closed-snapshot-attestations.json"),
        {
            "pre_restart": _snapshot_attestation(before_snapshot),
            "post_restart": _snapshot_attestation(after_snapshot),
            "raw_snapshots_retained_in_external_staging": True,
            "exclusion_reason": (
                "Tenant-wide raw SQLite contains unrelated security-control records; "
                "the sealed zero-secret bundle retains hashes and campaign-scoped validation."
            ),
        },
    )
    store.ingest_artifact(provisioning_manifest, "handoff/provisioning-manifest.json")
    store.ingest_artifact(staging_manifest, "handoff/staging-manifest.json")
    store._write_exclusive(Path("runtime/raw-playwright-summary.json"), raw_summary)
    store._write_exclusive(
        Path("runtime/validated-playwright-summary.json"), validated_summary
    )
    store._write_exclusive(Path("runtime/snapshot-validation.json"), snapshot_validation)
    store._write_exclusive(Path("playwright-report/evidence-index.json"), browser_index)
    for source, relative in browser_artifacts:
        store.ingest_artifact(source, relative)
        paths.append(relative.as_posix())
    event_id = store.append_event(
        "campaign.d012.child_approval.verified",
        {
            "result": "pass",
            "fixture_id": fixture.fixture_id,
            "approval_parent_run_ids": validated_summary["parent_run_ids"],
            "durable_sibling_replay_count": 0,
            "signed_continuation_count": 2,
            "provider_calls_performed": 0,
            "total_cost_usd": 0.0,
            "provider_economics_status": "blocked",
        },
        correlation=CorrelationIds(run_id=staged.parent_run_id),
    )
    evidence = tuple([*paths, f"events.ndjson#{event_id}"])
    acceptance = [
        *(AcceptanceCriterion(criterion, "pass", evidence) for criterion in _CRITERIA),
        AcceptanceCriterion(
            "economics.provider-measured",
            "blocked",
            evidence,
            "Provider-free D-012 checkpoint made no provider call; no provider economics claimed.",
        ),
    ]
    store.finalize_bundle(
        acceptance=acceptance,
        report_markdown=(
            "# D-012 child-owned approval persistence checkpoint\n\n"
            "The parent-scoped Approvals UI exposed the exact child-owned approval after one "
            "backend restart. Approve and reject reasons were persisted; each exact child "
            "continued once, the already-delivered sibling was not replayed, and signed audit "
            "linkage reconciled with closed pre/post SQLite snapshots.\n\n"
            "This checkpoint is provider-free. It proves zero priced calls and zero recorded "
            "cost, while measured provider economics remains explicitly blocked. Closed raw "
            "SQLite snapshots remain in the external staging root; the sealed bundle contains "
            "their SHA-256 attestations and campaign-scoped reconciliation because unrelated "
            "tenant-wide security records are excluded by the zero-secret evidence boundary.\n"
        ),
    )
    return destination


def _client(args: argparse.Namespace) -> HttpFixtureClient:
    api_key = os.environ.get("ZEROTH_EVALUATION_API_KEY")
    if not api_key:
        raise RuntimeError("ZEROTH_EVALUATION_API_KEY must come from the external environment")
    return HttpFixtureClient(base_url=args.api_base, api_key=api_key, tenant_id=args.tenant)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    provision = subparsers.add_parser("provision")
    provision.add_argument("--api-base", required=True)
    provision.add_argument("--tenant", required=True)
    provision.add_argument("--fixture-id", required=True)
    provision.add_argument("--manifest", type=Path, required=True)
    import_fixture = subparsers.add_parser("import-fixture")
    import_fixture.add_argument("--api-base", required=True)
    import_fixture.add_argument("--tenant", required=True)
    import_fixture.add_argument("--fixture-id", required=True)
    import_fixture.add_argument("--manifest", type=Path, required=True)
    stage = subparsers.add_parser("stage")
    stage.add_argument("--api-base", required=True)
    stage.add_argument("--tenant", required=True)
    stage.add_argument("--fixture-manifest", type=Path, required=True)
    stage.add_argument("--container-started-at", required=True)
    stage.add_argument("--manifest", type=Path, required=True)
    run_ui = subparsers.add_parser("run-ui")
    run_ui.add_argument("--frontend-root", type=Path, required=True)
    run_ui.add_argument("--fixture-manifest", type=Path, required=True)
    run_ui.add_argument("--staging-manifest", type=Path, required=True)
    run_ui.add_argument("--container-started-at-after", required=True)
    run_ui.add_argument("--browser-root", type=Path, required=True)
    run_ui.add_argument("--result-manifest", type=Path, required=True)
    seal = subparsers.add_parser("seal")
    seal.add_argument("--tenant", required=True)
    seal.add_argument("--fixture-manifest", type=Path, required=True)
    seal.add_argument("--staging-manifest", type=Path, required=True)
    seal.add_argument("--ui-result-manifest", type=Path, required=True)
    seal.add_argument("--before-snapshot", type=Path, required=True)
    seal.add_argument("--after-snapshot", type=Path, required=True)
    seal.add_argument("--browser-root", type=Path, required=True)
    seal.add_argument("--destination", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "provision":
        fixture = provision_child_approval_fixture(
            request=_client(args), fixture_id=args.fixture_id
        )
        write_provisioning_manifest(args.manifest, fixture)
        result = {
            "manifest": str(args.manifest),
            "parent_deployment_ref": fixture.parent_deployment_ref,
            "parent_graph_version_ref": fixture.parent_graph_version_ref,
        }
    elif args.command == "import-fixture":
        fixture = recover_child_approval_fixture(
            request=_client(args), fixture_id=args.fixture_id
        )
        write_provisioning_manifest(args.manifest, fixture)
        result = {
            "manifest": str(args.manifest),
            "parent_deployment_ref": fixture.parent_deployment_ref,
            "parent_graph_version_ref": fixture.parent_graph_version_ref,
            "reprovisioned": False,
        }
    elif args.command == "stage":
        fixture = read_provisioning_manifest(args.fixture_manifest)
        staged = stage_pending_child_approval(
            request=_client(args),
            fixture=fixture,
            container_started_at=args.container_started_at,
        )
        write_staging_manifest(args.manifest, fixture, staged)
        result = {"manifest": str(args.manifest), "parent_run_id": staged.parent_run_id}
    elif args.command == "run-ui":
        fixture = read_provisioning_manifest(args.fixture_manifest)
        staged_fixture, staged = read_staging_manifest(args.staging_manifest)
        if fixture != staged_fixture:
            raise RuntimeError("D-012 run-ui manifests disagree")
        environment = {
            key: value
            for key in (
                "ZEROTH_EVALUATION_API_BASE",
                "ZEROTH_EVALUATION_API_KEY",
                "ZEROTH_EVALUATION_BASE_URL",
                "ZEROTH_EVALUATION_TENANT",
            )
            if (value := os.environ.get(key)) is not None
        }
        environment["ZEROTH_EVALUATION_BROWSER_ROOT"] = str(args.browser_root)
        runner = BoundedChildApprovalUiRunner(
            frontend_root=args.frontend_root,
            environment=environment,
        )
        ui_result = runner.run(
            fixture,
            staged=staged,
            container_started_at_after=args.container_started_at_after,
        )
        write_ui_result_manifest(args.result_manifest, ui_result)
        result = {"result_manifest": str(args.result_manifest), "status": "validated"}
    else:
        root = seal_child_approval_checkpoint(
            destination=args.destination,
            tenant_id=args.tenant,
            provisioning_manifest=args.fixture_manifest,
            staging_manifest=args.staging_manifest,
            ui_result_manifest=args.ui_result_manifest,
            before_snapshot=args.before_snapshot,
            after_snapshot=args.after_snapshot,
            browser_root=args.browser_root,
        )
        result = {"root": str(root), "sealed": EvidenceStore(root).is_sealed}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
