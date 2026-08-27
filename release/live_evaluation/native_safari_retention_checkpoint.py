"""Seal native Safari Retention validation without mutating retention state."""

from __future__ import annotations

import json
import plistlib
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .workflow3_lifecycle_evidence import STATE_ROOT, WORKTREE, _request, _tree_digest

TENANT = "evaluation-studio-v1"
ROLE = "platform_admin"
DEPLOYMENT = "evaluation-studio-v1-grounded-researcher-v1"
GRAPH = "evaluation-studio-v1-grounded-researcher@4"
HOLD_ID = "8d452480319d4578895007cc8a36c8f0"
HELD_RUN_ID = "379e3364e2184e93abef39db8cbd3d44"
ROUTE = "/retention"

SOURCE_ROOT = STATE_ROOT / "evidence/native-safari-retention-validation-staging-20260825-1"
ROOT = STATE_ROOT / "evidence/native-safari-retention-validation-checkpoint-20260825-1"

ACCEPTED_CRITERIA = (
    "product.retention.native-safari-paint",
    "product.retention.invalid-ttl-validation",
    "product.retention.refresh-restoration",
)

ARTIFACT_STEMS = (
    "01-invalid-ttl-native-safari",
    "02-restored-before-refresh-native-safari",
    "03-restored-after-refresh-native-safari",
)

Request = Callable[..., Any]


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be a JSON object")
    return value


def _sequence(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"{label} must be a JSON array of objects")
    return value


def _sanitize_runtime(request: Request) -> dict[str, Any]:
    health = _object(request("/health"), label="health")
    identity = _object(request("/v1/identity"), label="identity")
    policy = _object(request("/v1/retention/policy"), label="retention policy")
    holds = _sequence(request("/v1/retention/legal-holds"), label="legal holds")
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
        "retention-policy": {
            key: policy.get(key)
            for key in (
                "tenant_id",
                "enabled",
                "run_ttl_seconds",
                "audit_ttl_seconds",
            )
        },
        "legal-holds": [
            {
                key: hold.get(key)
                for key in (
                    "hold_id",
                    "tenant_id",
                    "run_id",
                    "reason",
                    "placed_by",
                    "active",
                )
            }
            for hold in holds
        ],
    }


def _validate_runtime(records: Mapping[str, Any]) -> None:
    health = records["health"]
    if not isinstance(health, Mapping) or {
        "status": health.get("status"),
        "campaign_id": health.get("campaign_id"),
        "deployment_ref": health.get("deployment_ref"),
        "graph_version_ref": health.get("graph_version_ref"),
    } != {
        "status": "ok",
        "campaign_id": TENANT,
        "deployment_ref": DEPLOYMENT,
        "graph_version_ref": GRAPH,
    }:
        raise RuntimeError("health does not prove the exact served deployment")
    version = health.get("deployment_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RuntimeError("health has no valid deployment version")

    identity = records["identity"]
    if (
        not isinstance(identity, Mapping)
        or identity.get("tenant_id") != TENANT
        or identity.get("workspace_id") is not None
        or identity.get("roles") != [ROLE]
        or not isinstance(identity.get("subject"), str)
        or not identity.get("subject")
    ):
        raise RuntimeError("identity tenant and role do not match the Safari scope")

    policy = records["retention-policy"]
    if not isinstance(policy, Mapping) or dict(policy) != {
        "tenant_id": TENANT,
        "enabled": True,
        "run_ttl_seconds": None,
        "audit_ttl_seconds": None,
    }:
        raise RuntimeError("retention policy is not the exact restored no-expiry state")

    holds = records["legal-holds"]
    if not isinstance(holds, list):
        raise RuntimeError("legal holds are unavailable")
    matching = [
        hold for hold in holds if isinstance(hold, Mapping) and hold.get("hold_id") == HOLD_ID
    ]
    if len(matching) != 1 or {
        "tenant_id": matching[0].get("tenant_id"),
        "run_id": matching[0].get("run_id"),
        "active": matching[0].get("active"),
    } != {"tenant_id": TENANT, "run_id": HELD_RUN_ID, "active": True}:
        raise RuntimeError("the persisted run-scoped legal hold is unavailable")


def _valid_jpeg(path: Path) -> bool:
    return (
        not path.is_symlink() and path.is_file() and path.read_bytes().startswith(b"\xff\xd8\xff")
    )


def _validate_browser(source_root: Path) -> tuple[Path, ...]:
    artifacts: list[Path] = []
    texts: list[str] = []
    for stem in ARTIFACT_STEMS:
        screenshot = source_root / "screenshots" / f"{stem}.jpg"
        accessibility = source_root / "accessibility" / f"{stem}.txt"
        if not _valid_jpeg(screenshot):
            raise RuntimeError(f"invalid native Safari screenshot: {stem}")
        if accessibility.is_symlink() or not accessibility.is_file():
            raise RuntimeError(f"missing native Safari accessibility snapshot: {stem}")
        text = accessibility.read_text()
        if "127.0.0.1:3000/console/retention/" not in text:
            raise RuntimeError("native Safari artifact is not the Retention route")
        artifacts.extend((screenshot, accessibility))
        texts.append(text)

    invalid, before, after = texts
    if not all(
        token in invalid
        for token in (
            "Run payloads TTL in days, Value: -1",
            "text invalid",
            "button (disabled) Save policy",
            "TTL must be greater than zero days (blank = no expiry).",
        )
    ):
        raise RuntimeError("invalid TTL evidence is incomplete or unassociated")

    required_restored = (
        "http://127.0.0.1:8122",
        "Retention & Compliance",
        "evaluation-studio-v1",
        "Retention enforcement enabled, Value: 1",
        "button (disabled) Save policy",
        HOLD_ID,
        HELD_RUN_ID,
        "TTLs suspended",
    )
    for text in (before, after):
        if (
            not all(token in text for token in required_restored)
            or text.count("no expiry") < 2
            or "Value: -1" in text
            or "TTL must be greater than zero" in text
        ):
            raise RuntimeError("native Safari refresh did not restore exact Retention state")
    return tuple(artifacts)


def _safari_version() -> str:
    info = Path("/Applications/Safari.app/Contents/Info.plist")
    if not info.is_file():
        return "unknown"
    with info.open("rb") as stream:
        value = plistlib.load(stream).get("CFBundleShortVersionString")
    return value if isinstance(value, str) and value else "unknown"


def _revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=WORKTREE,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_checkpoint(*, source_root: Path, destination: Path, request: Request) -> Path:
    """Validate the native UI/runtime join and seal a secret-clean bundle."""
    source_root = source_root.expanduser().resolve(strict=True)
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)

    records = _sanitize_runtime(request)
    _validate_runtime(records)
    browser_artifacts = _validate_browser(source_root)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "native-safari-retention-validation",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": _revision(),
            "working_tree_digest": _tree_digest(WORKTREE),
            "tenant_id": TENANT,
            "workspace_id": None,
            "role": ROLE,
            "route": ROUTE,
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "browser": {"name": "Safari", "version": _safari_version()},
            "provider_calls_performed": 0,
            "mutations_performed": 0,
            "native_safari_screenshot_count": len(ARTIFACT_STEMS),
            "native_safari_accessibility_snapshot_count": len(ARTIFACT_STEMS),
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    evidence_paths: list[str] = []
    for name, value in records.items():
        relative = Path("runtime") / f"{name}.json"
        store._write_exclusive(relative, value)
        evidence_paths.append(relative.as_posix())
    for source in browser_artifacts:
        relative = Path(source.parent.name) / source.name
        store.ingest_artifact(source, relative)
        evidence_paths.append(relative.as_posix())

    screenshot_index = {
        "schema_version": 1,
        "screenshots": [
            {
                "file": "screenshots/01-invalid-ttl-native-safari.jpg",
                "route": ROUTE,
                "control_ids": [
                    "retention.policy.run-payloads-ttl",
                    "retention.policy.save",
                ],
                "role": ROLE,
                "tenant_id": TENANT,
                "expected_result": "validation_rejected_without_mutation",
            },
            {
                "file": "screenshots/02-restored-before-refresh-native-safari.jpg",
                "route": ROUTE,
                "control_ids": ["retention.policy.card", "retention.legal-holds.card"],
                "role": ROLE,
                "tenant_id": TENANT,
                "expected_result": "restored_no_expiry_policy_and_active_hold",
            },
            {
                "file": "screenshots/03-restored-after-refresh-native-safari.jpg",
                "route": ROUTE,
                "control_ids": ["retention.policy.card", "retention.legal-holds.card"],
                "role": ROLE,
                "tenant_id": TENANT,
                "expected_result": "same_state_after_native_browser_refresh",
            },
        ],
    }
    store._write_exclusive(Path("screenshot-index.json"), screenshot_index)
    evidence_paths.append("screenshot-index.json")

    event_id = store.append_event(
        "campaign.native_safari.retention_verified",
        {
            "result": "pass",
            "route": ROUTE,
            "role": ROLE,
            "tenant_id": TENANT,
            "invalid_control_id": "retention.policy.run-payloads-ttl",
            "policy_state": "enabled_no_expiry",
            "active_hold_id": HOLD_ID,
            "mutations_performed": 0,
            "provider_call_count": 0,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(run_id=HELD_RUN_ID),
    )
    acceptance_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", acceptance_evidence)
            for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Native Safari Retention validation checkpoint\n\n"
            "Native Safari visibly painted the connected Retention & Compliance page "
            f"for tenant `{TENANT}` and role `{ROLE}`. The run-payload TTL field rejected "
            "`-1`, exposed an associated error, and left Save disabled. The field was "
            "restored without submitting a mutation. A real Safari reload restored the "
            "enabled no-expiry policy and the same active run-scoped legal hold. Runtime "
            "identity, policy, and hold records match the UI. No provider call or retention "
            "mutation occurred. Destructive erasure remains outside this checkpoint.\n"
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
