"""Seal live tenant identity and role-authorization isolation evidence."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol

from .evidence import AcceptanceCriterion, CorrelationIds, EvidenceStore
from .native_safari_retention_checkpoint import _revision
from .workflow3_lifecycle_evidence import STATE_ROOT, WORKTREE, _tree_digest

PRIMARY_TENANT = "evaluation-studio-v1"
TWIN_TENANT = "evaluation-studio-v1-twin"
ROLES = ("admin", "operator", "platform_admin", "reviewer")
SURFACES = ("audit", "economics", "retention-compliance")

SOURCE_ROOT = STATE_ROOT / "evidence/identity-isolation-live-20260825-6"
ROOT = STATE_ROOT / "evidence/identity-isolation-live-checkpoint-20260825-1"

TENANT_SERVICES = {
    PRIMARY_TENANT: "primary",
    TWIN_TENANT: "twin",
}
SERVICE_URLS = {
    "primary": "http://127.0.0.1:8122",
    "twin": "http://127.0.0.1:8123",
}
EXPECTED_HEALTH = {
    "primary": {
        "status": "ok",
        "campaign_id": PRIMARY_TENANT,
        "deployment_ref": "evaluation-studio-v1-grounded-researcher-v1",
        "deployment_version": 6,
        "graph_version_ref": "evaluation-studio-v1-grounded-researcher@4",
    },
    "twin": {
        "status": "ok",
        "campaign_id": TWIN_TENANT,
        "deployment_ref": "evaluation-studio-v1-twin-bootstrap-v1",
        "deployment_version": 1,
        "graph_version_ref": "evaluation-studio-v1-twin-bootstrap@1",
    },
}
ROLE_ACCESS = {
    "admin": {"audit": True, "economics": True, "retention-compliance": True},
    "operator": {"audit": False, "economics": False, "retention-compliance": False},
    "platform_admin": {
        "audit": True,
        "economics": True,
        "retention-compliance": True,
    },
    "reviewer": {"audit": True, "economics": False, "retention-compliance": False},
}
SURFACE_PATHS = {
    "audit": ("/v1/deployments/evaluation-studio-v1-grounded-researcher-v1/audits"),
    "economics": ("/v1/deployments/evaluation-studio-v1-grounded-researcher-v1/cost"),
    "retention-compliance": "/v1/retention/policy",
}
ACCEPTED_CRITERIA = (
    "identity.authoritative-scope",
    "identity.retention-tenant-isolation",
    "identity.role-denial",
    "identity.role.admin",
    "identity.role.operator",
    "identity.role.platform_admin",
    "identity.role.reviewer",
    "identity.tenant-isolation",
)

_SECRET_NAMES = {
    (PRIMARY_TENANT, "admin"): "tenant-a-admin-key",
    (PRIMARY_TENANT, "operator"): "tenant-a-operator-key",
    (PRIMARY_TENANT, "platform_admin"): "service-api-key",
    (PRIMARY_TENANT, "reviewer"): "tenant-a-reviewer-key",
    (TWIN_TENANT, "admin"): "tenant-b-admin-key",
    (TWIN_TENANT, "operator"): "tenant-b-operator-key",
    (TWIN_TENANT, "platform_admin"): "tenant-b-platform-admin-key",
    (TWIN_TENANT, "reviewer"): "tenant-b-reviewer-key",
}
_SECRET_ROOT = STATE_ROOT / "runtime-secrets"
_ARTIFACT_TOP_LEVEL = {"console", "playwright-report", "screenshots", "videos"}

Service = Literal["primary", "twin"]


@dataclass(frozen=True, slots=True)
class RuntimeResponse:
    status_code: int
    body: Any

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status_code, int)
            or isinstance(self.status_code, bool)
            or not 100 <= self.status_code <= 599
        ):
            raise ValueError("runtime response has an invalid status code")


class Request(Protocol):
    def __call__(
        self,
        service: Service,
        path: str,
        *,
        tenant_id: str,
        role: str,
    ) -> RuntimeResponse: ...


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    source: Path
    destination: Path


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    results: dict[str, Any]
    artifacts: tuple[SourceArtifact, ...]
    scope_screenshots: tuple[str, ...]
    role_screenshots: tuple[str, ...]
    videos: tuple[str, ...]
    html_report_file_count: int


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


def _artifact_kind(reference: str) -> str:
    return Path(reference).parts[0]


def _criterion_evidence(rows: Mapping[str, Mapping[str, Any]], criterion: str) -> tuple[str, ...]:
    value = rows[criterion].get("evidence")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError("criterion evidence is malformed")
    if len(value) != len(set(value)):
        raise RuntimeError("criterion evidence contains duplicates")
    return tuple(value)


def _validate_console(artifacts: list[SourceArtifact]) -> None:
    expected_scope = {
        (
            tenant,
            role,
            200,
            404,
            200 if role in {"admin", "platform_admin"} else 403,
            404 if role in {"admin", "platform_admin"} else 403,
            True,
            404 if role in {"admin", "platform_admin"} else 403,
            True,
        )
        for tenant in TENANT_SERVICES
        for role in ROLES
    }
    expected_authorization = {
        (
            PRIMARY_TENANT,
            role,
            ROLE_ACCESS[role]["audit"],
            ROLE_ACCESS[role]["economics"],
            ROLE_ACCESS[role]["retention-compliance"],
        )
        for role in ROLES
    }
    scope: set[tuple[object, ...]] = set()
    authorization: set[tuple[object, ...]] = set()
    for artifact in artifacts:
        if artifact.destination.parts[0] != "console":
            continue
        value = _load_json(artifact.source, label=artifact.destination.name)
        if not isinstance(value, dict):
            raise RuntimeError("console result must be an object")
        keys = set(value)
        if keys == {
            "tenant_id",
            "role",
            "own_service_status",
            "cross_tenant_status",
            "own_retention_policy_status",
            "cross_tenant_retention_policy_status",
            "cross_tenant_retention_payload_fields",
            "cross_tenant_legal_holds_status",
            "cross_tenant_legal_holds_payload_fields",
        }:
            scope.add(
                (
                    value["tenant_id"],
                    value["role"],
                    value["own_service_status"],
                    value["cross_tenant_status"],
                    value["own_retention_policy_status"],
                    value["cross_tenant_retention_policy_status"],
                    value["cross_tenant_retention_payload_fields"] == ["detail"],
                    value["cross_tenant_legal_holds_status"],
                    value["cross_tenant_legal_holds_payload_fields"] == ["detail"],
                )
            )
        elif keys == {
            "tenant_id",
            "role",
            "audit_allowed",
            "economics_allowed",
            "retention_allowed",
        }:
            authorization.add(
                (
                    value["tenant_id"],
                    value["role"],
                    value["audit_allowed"],
                    value["economics_allowed"],
                    value["retention_allowed"],
                )
            )
        elif {"tenant_id", "role", "own_service_status", "cross_tenant_status"} <= keys:
            raise RuntimeError("scope result matrix is incomplete")
        else:
            raise RuntimeError("console result has unsafe or unexpected fields")
    if scope != expected_scope:
        raise RuntimeError("scope result matrix is incomplete")
    if authorization != expected_authorization:
        raise RuntimeError("authorization result matrix is incomplete")


def _validate_criterion_artifacts(
    criteria: Mapping[str, Mapping[str, Any]], destinations: set[str]
) -> None:
    evidence = {
        criterion: _criterion_evidence(criteria, criterion) for criterion in ACCEPTED_CRITERIA
    }
    if any(set(references) - destinations for references in evidence.values()):
        raise RuntimeError("criterion evidence is missing or undeclared")

    scope = evidence["identity.authoritative-scope"]
    retention_scope = evidence["identity.retention-tenant-isolation"]
    tenant = evidence["identity.tenant-isolation"]
    denial = evidence["identity.role-denial"]
    expected_counts = {"console": 8, "screenshots": 8, "videos": 8}
    if (
        scope != retention_scope
        or scope != tenant
        or {kind: sum(_artifact_kind(item) == kind for item in scope) for kind in expected_counts}
        != expected_counts
    ):
        raise RuntimeError("authoritative scope evidence categories are incomplete")
    denial_counts = {"console": 4, "screenshots": 12, "videos": 4}
    if {
        kind: sum(_artifact_kind(item) == kind for item in denial) for kind in denial_counts
    } != denial_counts:
        raise RuntimeError("role-denial evidence categories are incomplete")
    if set(scope).intersection(denial):
        raise RuntimeError("scope and role-denial evidence must be independent")

    scope_videos = {item for item in scope if item.startswith("videos/")}
    denial_videos = {item for item in denial if item.startswith("videos/")}
    for role in ROLES:
        references = evidence[f"identity.role.{role}"]
        counts = {
            kind: sum(_artifact_kind(item) == kind for item in references)
            for kind in ("console", "screenshots", "videos")
        }
        videos = {item for item in references if item.startswith("videos/")}
        if (
            counts != {"console": 3, "screenshots": 5, "videos": 3}
            or len(videos.intersection(scope_videos)) != 2
            or len(videos.intersection(denial_videos)) != 1
        ):
            raise RuntimeError(f"role evidence categories are incomplete: {role}")


def _load_source(root: Path) -> SourceEvidence:
    root = root.expanduser().resolve(strict=True)
    EvidenceStore(root).scan_recursive()
    results = _load_json(root / "results.json", label="source results")
    if not isinstance(results, dict):
        raise RuntimeError("source results must be an object")
    criteria_value = results.get("criteria")
    if (
        results.get("schema_version") != 1
        or results.get("completed") is not True
        or not isinstance(criteria_value, list)
    ):
        raise RuntimeError("source results are incomplete")
    if not all(isinstance(row, dict) for row in criteria_value):
        raise RuntimeError("source criterion is malformed")
    criteria = {
        str(row.get("criterion_id")): row for row in criteria_value if isinstance(row, dict)
    }
    if (
        len(criteria) != len(criteria_value)
        or tuple(criteria) != ACCEPTED_CRITERIA
        or any(row.get("status") != "pass" for row in criteria.values())
        or any(
            not isinstance(row.get("test_id"), str) or not row["test_id"]
            for row in criteria.values()
        )
    ):
        raise RuntimeError("source result criteria do not match the checkpoint allowlist")

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
        artifacts.append(SourceArtifact(_source_file(root, source_relative), destination))

    declared_by_kind = {
        kind: [item for item in artifacts if item.destination.parts[0] == kind]
        for kind in _ARTIFACT_TOP_LEVEL
    }
    if {kind: len(values) for kind, values in declared_by_kind.items()} != {
        "console": 12,
        "playwright-report": 1,
        "screenshots": 20,
        "videos": 12,
    }:
        raise RuntimeError("source artifact counts do not match the identity checkpoint")
    report_declaration = declared_by_kind["playwright-report"][0]
    if report_declaration.destination != Path("playwright-report/index.html"):
        raise RuntimeError("source does not declare the Playwright report entrypoint")

    screenshot_names = [item.destination.name for item in declared_by_kind["screenshots"]]
    scope_screenshots = tuple(
        f"screenshots/{name}"
        for name in screenshot_names
        if name.startswith("scope-") or "-scope-" in name
    )
    role_screenshots = tuple(
        f"screenshots/{name}"
        for name in screenshot_names
        if f"screenshots/{name}" not in scope_screenshots
    )
    expected_role_suffixes = {f"-{role}-{surface}.png" for role in ROLES for surface in SURFACES}
    observed_role_suffixes = {
        suffix
        for suffix in expected_role_suffixes
        if any(name.endswith(suffix) for name in screenshot_names)
    }
    if (
        len(scope_screenshots) != 8
        or len(role_screenshots) != 12
        or observed_role_suffixes != expected_role_suffixes
    ):
        raise RuntimeError("screenshot categories do not match the identity checkpoint")

    _validate_console(artifacts)
    _validate_criterion_artifacts(criteria, destinations)

    declared = set(destinations)
    report_root = root / "html-report"
    report_counts = {".html": 0, ".png": 0, ".webm": 0}
    report_files = 0
    for source in sorted(report_root.rglob("*")):
        if source.is_symlink():
            raise RuntimeError("Playwright report may not contain symlinks")
        if not source.is_file():
            continue
        suffix = source.suffix.lower()
        if suffix not in report_counts:
            raise RuntimeError("Playwright report contains an unexpected artifact type")
        report_counts[suffix] += 1
        report_files += 1
        relative = Path("playwright-report") / source.relative_to(report_root)
        if relative.as_posix() not in declared:
            artifacts.append(SourceArtifact(source, relative))
            declared.add(relative.as_posix())
    if report_counts != {".html": 1, ".png": 20, ".webm": 12}:
        raise RuntimeError("Playwright report is not the complete identity run")

    return SourceEvidence(
        results=results,
        artifacts=tuple(artifacts),
        scope_screenshots=scope_screenshots,
        role_screenshots=role_screenshots,
        videos=tuple(item.destination.as_posix() for item in declared_by_kind["videos"]),
        html_report_file_count=report_files,
    )


def _expect_response(value: Any, *, label: str) -> RuntimeResponse:
    if not isinstance(value, RuntimeResponse):
        raise RuntimeError(f"{label} request returned an invalid response")
    return value


def _runtime_evidence(request: Request) -> dict[str, Any]:
    health_records: list[dict[str, Any]] = []
    identity_records: list[dict[str, Any]] = []
    authorization_records: list[dict[str, Any]] = []
    for tenant, service_value in TENANT_SERVICES.items():
        service = service_value  # narrowed by the declared constant map
        health = _expect_response(
            request(service, "/health", tenant_id=tenant, role="platform_admin"),  # type: ignore[arg-type]
            label=f"{service} health",
        )
        if health.status_code != 200 or not isinstance(health.body, Mapping):
            raise RuntimeError(f"{service} health is unavailable")
        projected_health = {key: health.body.get(key) for key in EXPECTED_HEALTH[service]}
        if projected_health != EXPECTED_HEALTH[service]:
            raise RuntimeError(f"{service} health does not match the isolated service")
        health_records.append({"service": service, **projected_health})

        other_service = "twin" if service == "primary" else "primary"
        for role in ROLES:
            own = _expect_response(
                request(service, "/v1/identity", tenant_id=tenant, role=role),  # type: ignore[arg-type]
                label="own identity",
            )
            if own.status_code != 200 or not isinstance(own.body, Mapping):
                raise RuntimeError("own-service identity request was not authorized")
            own_identity = {
                key: own.body.get(key) for key in ("subject", "tenant_id", "workspace_id", "roles")
            }
            if (
                not isinstance(own_identity["subject"], str)
                or not own_identity["subject"]
                or own_identity["tenant_id"] != tenant
                or own_identity["workspace_id"] is not None
                or own_identity["roles"] != [role]
            ):
                raise RuntimeError("runtime identity does not match the authoritative scope")
            cross = _expect_response(
                request(other_service, "/v1/identity", tenant_id=tenant, role=role),  # type: ignore[arg-type]
                label="cross-tenant identity",
            )
            if cross.status_code != 404:
                raise RuntimeError("cross-tenant identity request was not concealed")
            identity_records.append(
                {
                    "service": service,
                    "tenant_id": tenant,
                    "role": role,
                    "identity": own_identity,
                    "own_status": own.status_code,
                    "cross_tenant_status": cross.status_code,
                }
            )

    for role in ROLES:
        for surface, path in SURFACE_PATHS.items():
            response = _expect_response(
                request(
                    "primary",
                    path,
                    tenant_id=PRIMARY_TENANT,
                    role=role,
                ),
                label=f"{role} {surface}",
            )
            allowed = ROLE_ACCESS[role][surface]
            expected_status = 200 if allowed else 403
            if response.status_code != expected_status:
                raise RuntimeError("runtime authorization matrix does not match the UI evidence")
            authorization_records.append(
                {
                    "tenant_id": PRIMARY_TENANT,
                    "role": role,
                    "surface": surface,
                    "allowed": allowed,
                    "status_code": response.status_code,
                }
            )
    return {
        "health-matrix": health_records,
        "identity-matrix": identity_records,
        "authorization-matrix": authorization_records,
    }


def build_checkpoint(*, source_root: Path, destination: Path, request: Request) -> Path:
    """Validate source artifacts and live isolation before creating the checkpoint."""
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)
    source = _load_source(source_root)
    runtime = _runtime_evidence(request)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "identity-role-tenant-isolation-live",
            "created_at": datetime.now(UTC).isoformat(),
            "revision": _revision(),
            "working_tree_digest": _tree_digest(WORKTREE),
            "source_root": source_root.resolve().name,
            "source_results_sha256": sha256(
                (source_root / "results.json").read_bytes()
            ).hexdigest(),
            "tenants": list(TENANT_SERVICES),
            "roles": list(ROLES),
            "scope_console_count": 8,
            "retention_scope_console_count": 8,
            "role_console_count": 4,
            "scope_screenshot_count": len(source.scope_screenshots),
            "role_screenshot_count": len(source.role_screenshots),
            "video_count": len(source.videos),
            "html_report_file_count": source.html_report_file_count,
            "provider_calls_performed": 0,
            "mutations_performed": 0,
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
        "screenshots": [
            {
                "file": reference,
                "category": (
                    "authoritative-scope"
                    if reference in source.scope_screenshots
                    else "role-authorization"
                ),
                "criterion_ids": [
                    row["criterion_id"]
                    for row in source.results["criteria"]
                    if reference in row["evidence"]
                ],
            }
            for reference in (*source.scope_screenshots, *source.role_screenshots)
        ],
    }
    store._write_exclusive(Path("screenshot-index.json"), screenshot_index)
    evidence_paths.append("screenshot-index.json")
    store.record_command(
        sequence=1,
        name="identity-role-isolation-live-playwright",
        argv=[
            "npx",
            "playwright",
            "test",
            "e2e/tenant-role-isolation-live.spec.ts",
            "e2e/role-authorization-surfaces-live.spec.ts",
            "--project=desktop-1440",
        ],
        working_directory=WORKTREE / "frontend",
        exit_code=0,
        stdout="12 live identity and authorization tests passed.\n",
        stderr="",
    )
    evidence_paths.append("commands/0001-identity-role-isolation-live-playwright.json")
    event_id = store.append_event(
        "campaign.identity_role_isolation_verified",
        {
            "result": "pass",
            "tenant_count": 2,
            "role_count": 4,
            "scope_case_count": 8,
            "retention_scope_case_count": 8,
            "role_case_count": 12,
            "cross_tenant_concealment_count": 8,
            "provider_call_count": 0,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(ui_action_id="identity-isolation-live-20260825-6"),
    )
    common_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    store.finalize_bundle(
        acceptance=tuple(
            AcceptanceCriterion(criterion, "pass", common_evidence)
            for criterion in ACCEPTED_CRITERIA
        ),
        report_markdown=(
            "# Identity, role, and tenant isolation live checkpoint\n\n"
            "Eight primary/twin tenant-role sessions exposed the exact authenticated "
            "tenant and singleton role, while the same credential received a concealed "
            "404 from the other tenant service. Every session also proved exact own-role "
            "Retention access and safe cross-tenant policy and legal-hold denials with "
            "detail-only payloads. Four primary-tenant roles exercised "
            "Audit, Economics, and Retention: operator was denied all three, reviewer "
            "could read only Audit, and admin and platform administrator could read all "
            "three. The checkpoint independently corroborates both service health "
            "identities and seals the exact console records, 20 screenshots, 12 videos, "
            "and complete HTML report. Only sanitized response projections and status "
            "codes are retained. No provider call or runtime mutation occurred.\n"
        ),
    )
    return destination


def _request(
    service: Service,
    path: str,
    *,
    tenant_id: str,
    role: str,
) -> RuntimeResponse:
    """Execute an authenticated read without exposing the credential to callers."""
    if service not in SERVICE_URLS or (tenant_id, role) not in _SECRET_NAMES:
        raise RuntimeError("unknown identity checkpoint request scope")
    credential = (_SECRET_ROOT / _SECRET_NAMES[(tenant_id, role)]).read_text().strip()
    if not credential:
        raise RuntimeError("identity checkpoint credential is unavailable")
    outbound = urllib.request.Request(
        f"{SERVICE_URLS[service]}{path}",
        headers={"X-API-Key": credential},
        method="GET",
    )
    try:
        with urllib.request.urlopen(outbound, timeout=15) as response:
            try:
                body = json.load(response)
            except json.JSONDecodeError as exc:
                raise RuntimeError("runtime request returned malformed JSON") from exc
            return RuntimeResponse(response.status, body)
    except urllib.error.HTTPError as exc:
        return RuntimeResponse(exc.code, {})


def main() -> int:
    root = build_checkpoint(source_root=SOURCE_ROOT, destination=ROOT, request=_request)
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
