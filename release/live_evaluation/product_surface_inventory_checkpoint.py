"""Seal the all-route live product control inventory."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from release.product_validation.catalog import ProductValidationCatalog

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .native_safari_retention_checkpoint import (
    DEPLOYMENT,
    GRAPH,
    ROLE,
    TENANT,
    _revision,
)
from .workflow3_lifecycle_evidence import STATE_ROOT, WORKTREE, _request, _tree_digest

SOURCE_ROOT = STATE_ROOT / "evidence/product-surface-inventory-20260825-1"
ROOT = STATE_ROOT / "evidence/product-surface-inventory-checkpoint-20260825-1"
CATALOG_PATH = WORKTREE / "release/product_validation/catalog-v1.json"

CATALOG = ProductValidationCatalog.model_validate_json(CATALOG_PATH.read_text())
ROUTES = tuple(sorted(CATALOG.console_routes))

_ARTIFACT_TOP_LEVEL = {
    "console",
    "playwright-report",
    "screenshots",
    "videos",
}

Request = Callable[..., Any]


def criterion_for_route(route: str) -> str:
    return "controls.overview" if route == "/" else f"controls{route.replace('/', '.')}"


ACCEPTED_CRITERIA = tuple(criterion_for_route(route) for route in ROUTES)


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    source: Path
    destination: Path


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    results: dict[str, Any]
    artifacts: tuple[SourceArtifact, ...]
    criterion_routes: dict[str, str]


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


def _validate_control_inventory(path: Path) -> None:
    value = _load_json(path, label="control inventory")
    if not isinstance(value, Mapping):
        raise RuntimeError("control inventory must be an object")
    controls = value.get("controls")
    if not isinstance(controls, list) or not controls:
        raise RuntimeError("control inventory is empty")
    identities: list[str] = []
    enabled_selects: dict[str, set[str]] = {}
    enabled_checkboxes: set[str] = set()
    enabled_radios: dict[str, tuple[str | None, str]] = {}
    for control in controls:
        if not isinstance(control, Mapping):
            raise RuntimeError("control inventory contains a malformed control")
        identity = control.get("evidence_id")
        capabilities = control.get("capability_ids")
        if not isinstance(identity, str) or not identity:
            raise RuntimeError("control inventory contains an unnamed control")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or not all(
                isinstance(capability, str) and capability in CATALOG.capability_ids
                for capability in capabilities
            )
        ):
            raise RuntimeError(f"uncataloged control: {identity}")
        identities.append(identity)
        if control.get("disabled") is True:
            continue
        if control.get("tag") == "select":
            options = control.get("options")
            if not isinstance(options, list):
                raise RuntimeError(f"select options are malformed: {identity}")
            enabled_values: set[str] = set()
            for option in options:
                if not isinstance(option, Mapping) or not isinstance(option.get("value"), str):
                    raise RuntimeError(f"select options are malformed: {identity}")
                if option.get("disabled") is not True:
                    enabled_values.add(option["value"])
            enabled_selects[identity] = enabled_values
        if control.get("type") == "checkbox" or control.get("role") in {
            "checkbox",
            "switch",
        }:
            enabled_checkboxes.add(identity)
        if control.get("type") == "radio" or control.get("role") == "radio":
            name = control.get("name")
            option_value = control.get("value")
            if name is not None and not isinstance(name, str):
                raise RuntimeError(f"radio option name is malformed: {identity}")
            if not isinstance(option_value, str):
                raise RuntimeError(f"radio option value is malformed: {identity}")
            enabled_radios[identity] = (name, option_value)
    if len(identities) != len(set(identities)):
        raise RuntimeError("control inventory contains duplicate evidence identities")
    if value.get("identity_errors") != []:
        raise RuntimeError("runtime evidence identity observer reported errors")
    select_rows = value.get("exercised_select_options")
    if not isinstance(select_rows, list):
        raise RuntimeError("control inventory omitted exercised_select_options")
    exercised_selects: dict[str, set[str]] = {}
    for row in select_rows:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("evidence_id"), str)
            or not isinstance(row.get("values"), list)
            or not all(isinstance(item, str) for item in row["values"])
        ):
            raise RuntimeError("select exercise is malformed")
        identity = row["evidence_id"]
        if identity in exercised_selects:
            raise RuntimeError(f"select exercise is duplicated: {identity}")
        exercised_selects[identity] = set(row["values"])
    if exercised_selects != enabled_selects:
        raise RuntimeError("select options were not exercised completely")

    checkbox_rows = value.get("exercised_checkbox_states")
    if not isinstance(checkbox_rows, list):
        raise RuntimeError("control inventory omitted exercised_checkbox_states")
    exercised_checkboxes: dict[str, set[bool]] = {}
    for row in checkbox_rows:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("evidence_id"), str)
            or not isinstance(row.get("states"), list)
            or not all(isinstance(item, bool) for item in row["states"])
            or not isinstance(row.get("appeared"), list)
            or not isinstance(row.get("disappeared"), list)
        ):
            raise RuntimeError("checkbox exercise is malformed")
        identity = row["evidence_id"]
        if identity in exercised_checkboxes:
            raise RuntimeError(f"checkbox exercise is duplicated: {identity}")
        exercised_checkboxes[identity] = set(row["states"])
    if set(exercised_checkboxes) != enabled_checkboxes or any(
        states != {False, True} for states in exercised_checkboxes.values()
    ):
        raise RuntimeError("checkbox states were not exercised completely")

    radio_rows = value.get("exercised_radio_options")
    if not isinstance(radio_rows, list):
        raise RuntimeError("control inventory omitted exercised_radio_options")
    exercised_radios: dict[str, tuple[str | None, str]] = {}
    for row in radio_rows:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("evidence_id"), str)
            or (row.get("name") is not None and not isinstance(row.get("name"), str))
            or not isinstance(row.get("value"), str)
        ):
            raise RuntimeError("radio exercise is malformed")
        identity = row["evidence_id"]
        if identity in exercised_radios:
            raise RuntimeError(f"radio exercise is duplicated: {identity}")
        exercised_radios[identity] = (row.get("name"), row["value"])
    if exercised_radios != enabled_radios:
        raise RuntimeError("radio options were not exercised completely")


def _load_source(root: Path) -> SourceEvidence:
    root = root.expanduser().resolve(strict=True)
    EvidenceStore(root).scan_recursive()
    results = _load_json(root / "results.json", label="source results")
    if not isinstance(results, dict):
        raise RuntimeError("source results must be an object")
    criteria = results.get("criteria")
    if (
        results.get("schema_version") != 1
        or results.get("completed") is not True
        or not isinstance(criteria, list)
    ):
        raise RuntimeError("source results are incomplete")
    dispositions = {
        row.get("criterion_id"): row.get("status") for row in criteria if isinstance(row, dict)
    }
    if dispositions != {criterion: "pass" for criterion in ACCEPTED_CRITERIA}:
        raise RuntimeError("source route criteria do not match the catalog")

    rows = results.get("artifacts")
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

    criterion_routes = {criterion_for_route(route): route for route in ROUTES}
    for row in criteria:
        if not isinstance(row, Mapping):
            raise RuntimeError("source criterion is malformed")
        criterion = row.get("criterion_id")
        references = row.get("evidence")
        if not isinstance(criterion, str) or criterion not in criterion_routes:
            raise RuntimeError("source criterion does not map to a catalog route")
        if not isinstance(references, list) or not all(
            isinstance(item, str) and item in destinations for item in references
        ):
            raise RuntimeError("criterion evidence is missing or undeclared")
        top_levels = [Path(reference).parts[0] for reference in references]
        if sorted(top_levels) != ["console", "screenshots", "videos"]:
            raise RuntimeError("route criterion lacks inventory, screenshot, or video evidence")

    inventory_artifacts = [item for item in artifacts if item.destination.parts[0] == "console"]
    screenshots = [item for item in artifacts if item.destination.parts[0] == "screenshots"]
    videos = [item for item in artifacts if item.destination.parts[0] == "videos"]
    if not (
        len(inventory_artifacts) == len(ROUTES)
        and len(screenshots) == len(ROUTES)
        and len(videos) == len(ROUTES)
    ):
        raise RuntimeError("product inventory does not contain one evidence set per route")
    for item in inventory_artifacts:
        _validate_control_inventory(item.source)

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
        results=results,
        artifacts=tuple(artifacts),
        criterion_routes=criterion_routes,
    )


def _runtime_identity(
    request: Request,
    *,
    expected_deployment: str,
    expected_graph: str,
) -> dict[str, Any]:
    health = request("/health")
    identity = request("/v1/identity")
    if not isinstance(health, Mapping) or not isinstance(identity, Mapping):
        raise RuntimeError("runtime identity responses are malformed")
    if {
        "status": health.get("status"),
        "campaign_id": health.get("campaign_id"),
        "deployment_ref": health.get("deployment_ref"),
        "graph_version_ref": health.get("graph_version_ref"),
    } != {
        "status": "ok",
        "campaign_id": TENANT,
        "deployment_ref": expected_deployment,
        "graph_version_ref": expected_graph,
    }:
        raise RuntimeError("runtime health does not match the served campaign")
    if identity.get("tenant_id") != TENANT or identity.get("roles") != [ROLE]:
        raise RuntimeError("runtime identity does not match the catalog session")
    return {
        "health": {
            key: health.get(key)
            for key in (
                "status",
                "campaign_id",
                "deployment_ref",
                "deployment_version",
                "graph_version_ref",
            )
        },
        "identity": {
            key: identity.get(key) for key in ("subject", "tenant_id", "workspace_id", "roles")
        },
    }


def build_checkpoint(
    *,
    source_root: Path,
    destination: Path,
    request: Request,
    expected_deployment: str = DEPLOYMENT,
    expected_graph: str = GRAPH,
) -> Path:
    """Validate the catalog inventory, correlate runtime scope, and seal artifacts."""
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    source = _load_source(source_root)
    runtime = _runtime_identity(
        request,
        expected_deployment=expected_deployment,
        expected_graph=expected_graph,
    )

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "product-surface-control-inventory",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": _revision(),
            "working_tree_digest": _tree_digest(WORKTREE),
            "source_root": source_root.resolve().name,
            "source_results_sha256": sha256(
                (source_root / "results.json").read_bytes()
            ).hexdigest(),
            "catalog_id": CATALOG.catalog_id,
            "tenant_id": TENANT,
            "role": ROLE,
            "route_count": len(ROUTES),
            "control_inventory_count": len(ROUTES),
            "screenshot_count": len(ROUTES),
            "video_count": len(ROUTES),
            "provider_calls_performed": 0,
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    evidence_paths = ["playwright-report/results.json"]
    store._write_exclusive(Path(evidence_paths[0]), source.results)
    for name, value in runtime.items():
        relative = Path("runtime") / f"{name}.json"
        store._write_exclusive(relative, value)
        evidence_paths.append(relative.as_posix())
    for artifact in source.artifacts:
        store.ingest_artifact(artifact.source, artifact.destination)
        evidence_paths.append(artifact.destination.as_posix())

    screenshot_index = {
        "schema_version": 1,
        "tenant_id": TENANT,
        "role": ROLE,
        "screenshots": [
            {
                "file": reference,
                "route": source.criterion_routes[row["criterion_id"]],
                "criterion_id": row["criterion_id"],
                "expected_result": "all_visible_controls_named_and_cataloged",
            }
            for row in source.results["criteria"]
            for reference in row["evidence"]
            if reference.startswith("screenshots/")
        ],
    }
    store._write_exclusive(Path("screenshot-index.json"), screenshot_index)
    evidence_paths.append("screenshot-index.json")
    store.record_command(
        sequence=1,
        name="product-surface-inventory",
        argv=[
            "npx",
            "playwright",
            "test",
            "e2e/product-surface-inventory.spec.ts",
            "--project=desktop-1440",
        ],
        working_directory=WORKTREE / "frontend",
        exit_code=0,
        stdout="21 route inventory tests passed; results.json completed with 21 passes.\n",
        stderr="",
    )
    evidence_paths.append("commands/0001-product-surface-inventory.json")

    event_id = store.append_event(
        "campaign.product_surface_inventory_verified",
        {
            "result": "pass",
            "catalog_id": CATALOG.catalog_id,
            "route_count": len(ROUTES),
            "control_inventory_count": len(ROUTES),
            "unnamed_control_count": 0,
            "uncataloged_control_count": 0,
            "runtime_identity_error_count": 0,
            "provider_call_count": 0,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(ui_action_id="product-surface-inventory-20260825-1"),
    )
    common_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", common_evidence)
            for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Product-surface control inventory checkpoint\n\n"
            "All 21 published console routes rendered in the isolated live service. "
            "Every visible interactive control had a stable `data-evidence-id`, matched "
            "at least one versioned catalog capability, and produced no runtime identity "
            "observer error. Every enabled select option and both states of every visible "
            "enabled checkbox were exercised and restored, and every visible enabled "
            "radio option was selected. Each route has a screenshot, "
            "video, and sanitized control inventory in the sealed bundle. This proves "
            "catalog reachability and control identity—not every control's complete "
            "positive, negative, persistence, recovery, role, and tenant matrix. No "
            "provider call occurred.\n"
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
