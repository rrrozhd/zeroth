"""Seal a resolved native Safari operator-authorization discrepancy."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .evidence import (
    AcceptanceCriterion,
    CorrelationIds,
    EvidenceStore,
    _assert_safe_file_payload,
    _validate_artifact_payload,
)
from .identity_isolation_live_checkpoint import _request as _scoped_request
from .workflow3_lifecycle_evidence import STATE_ROOT

TENANT = "evaluation-studio-v1"
ROLE = "operator"
ROUTE = "/retention"
DEPLOYMENT = "evaluation-studio-v1-grounded-researcher-v1"
DEPLOYMENT_VERSION = 6
GRAPH = "evaluation-studio-v1-grounded-researcher@4"

SOURCE_ROOT = STATE_ROOT / "evidence/native-safari-operator-denial-staging-20260825-1"
ROOT = STATE_ROOT / "evidence/native-safari-operator-denial-checkpoint-20260825-1"

ACCEPTED_CRITERIA = ("product.identity.native-safari-role-denial",)

BEFORE_STEM = "01-operator-retention-controls-leak-before-fix"
AFTER_STEM = "02-operator-retention-controls-hidden-after-fix"
HIDDEN_MESSAGE = (
    "Retention controls are hidden because this API key cannot read "
    "retention control administration data."
)
_ROLE_SCOPE = "Scope: evaluation-studio-v1 / tenant-wide; roles: operator"
_SERVED_DEPLOYMENT = f"operator local served:  {DEPLOYMENT}"
_CONTROL_BUTTON = re.compile(
    r"\bbutton(?: \(disabled\))? (?:Place hold|Stage erasure request|Release hold)\b",
    re.IGNORECASE,
)
_BEFORE_ERASURE_TOKENS = (
    "text Erasure requests",
    "button scope entire tenant",
    "button scope single run",
    "Stage erasure request",
)


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
    def __call__(self, path: str, *, method: str = "GET") -> RuntimeResponse: ...


@dataclass(frozen=True, slots=True)
class BrowserArtifact:
    source: Path
    destination: Path


def _expected_source_files() -> dict[Path, Path]:
    return {
        Path("accessibility") / f"{BEFORE_STEM}.txt": (
            Path("accessibility") / f"{BEFORE_STEM}.txt"
        ),
        Path("accessibility") / f"{AFTER_STEM}.txt": (Path("accessibility") / f"{AFTER_STEM}.txt"),
        # The native captures have JPEG bytes despite their staging suffix. The
        # checkpoint records their true media type rather than sealing a lie.
        Path("screenshots") / f"{BEFORE_STEM}.png": (Path("screenshots") / f"{BEFORE_STEM}.jpg"),
        Path("screenshots") / f"{AFTER_STEM}.png": (Path("screenshots") / f"{AFTER_STEM}.jpg"),
    }


def _source_file(root: Path, relative: Path) -> Path:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"missing native Safari artifact: {relative.as_posix()}")
    return path


def _validate_scope(text: str, *, label: str) -> None:
    if (
        "127.0.0.1:3000/console/retention/" not in text
        or _ROLE_SCOPE not in text
        or "evaluation-studio-v1 / tenant-wide / Retention" not in text
        or "Retention & Compliance" not in text
    ):
        raise RuntimeError(f"{label} does not prove native Safari operator scope")


def _validate_before(text: str) -> None:
    _validate_scope(text, label="before state")
    if (
        HIDDEN_MESSAGE not in text
        or "button Place hold" not in text
        or not all(token in text for token in _BEFORE_ERASURE_TOKENS)
    ):
        raise RuntimeError("before state does not prove the control leak")


def _validate_after(text: str) -> None:
    _validate_scope(text, label="after state")
    if _SERVED_DEPLOYMENT not in text:
        raise RuntimeError("after state does not prove the exact served deployment")
    if text.count(HIDDEN_MESSAGE) != 3:
        raise RuntimeError("after state does not show three explicit denial cards")
    if _CONTROL_BUTTON.search(text):
        raise RuntimeError("after state exposes retention mutation controls")


def _validate_browser(source_root: Path) -> tuple[BrowserArtifact, ...]:
    source_root = source_root.expanduser().resolve(strict=True)
    expected = _expected_source_files()
    for path in source_root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("native Safari staging evidence contains a symlink")
    actual = {path.relative_to(source_root) for path in source_root.rglob("*") if path.is_file()}
    if actual != set(expected):
        raise RuntimeError("native Safari staging artifact inventory does not match")

    artifacts: list[BrowserArtifact] = []
    texts: dict[str, str] = {}
    for source_relative, destination in expected.items():
        source = _source_file(source_root, source_relative)
        payload = source.read_bytes()
        if destination.suffix == ".jpg" and not payload.startswith(b"\xff\xd8\xff"):
            raise RuntimeError(f"invalid native Safari screenshot: {source_relative.name}")
        _validate_artifact_payload(payload, relative_path=destination)
        _assert_safe_file_payload(payload, relative_path=destination)
        if destination.suffix == ".txt":
            texts[destination.stem] = payload.decode("utf-8")
        artifacts.append(BrowserArtifact(source=source, destination=destination))

    _validate_before(texts[BEFORE_STEM])
    _validate_after(texts[AFTER_STEM])
    return tuple(artifacts)


def _response(value: Any, *, label: str) -> RuntimeResponse:
    if not isinstance(value, RuntimeResponse):
        raise RuntimeError(f"{label} request returned an invalid response")
    return value


def _runtime_evidence(request: Request) -> dict[str, Any]:
    health = _response(request("/health", method="GET"), label="health")
    identity = _response(request("/v1/identity", method="GET"), label="identity")
    policy = _response(request("/v1/retention/policy", method="GET"), label="retention policy")
    holds = _response(request("/v1/retention/legal-holds", method="GET"), label="legal holds")

    expected_health = {
        "status": "ok",
        "campaign_id": TENANT,
        "deployment_ref": DEPLOYMENT,
        "deployment_version": DEPLOYMENT_VERSION,
        "graph_version_ref": GRAPH,
    }
    if health.status_code != 200 or not isinstance(health.body, Mapping):
        raise RuntimeError("health is unavailable")
    health_projection = {key: health.body.get(key) for key in expected_health}
    if health_projection != expected_health:
        raise RuntimeError("health does not prove the exact served Workflow 1 deployment")

    if identity.status_code != 200 or not isinstance(identity.body, Mapping):
        raise RuntimeError("operator identity is unavailable")
    identity_projection = {
        key: identity.body.get(key) for key in ("subject", "tenant_id", "workspace_id", "roles")
    }
    if (
        not isinstance(identity_projection["subject"], str)
        or not identity_projection["subject"]
        or identity_projection["tenant_id"] != TENANT
        or identity_projection["workspace_id"] is not None
        or identity_projection["roles"] != [ROLE]
    ):
        raise RuntimeError("identity does not prove the current operator role")

    if policy.status_code != 403 or holds.status_code != 403:
        raise RuntimeError("policy and legal-hold reads were not both denied with HTTP 403")
    return {
        "health": health_projection,
        "identity": identity_projection,
        "denials": {
            "policy_status": policy.status_code,
            "legal_holds_status": holds.status_code,
        },
    }


def build_checkpoint(*, source_root: Path, destination: Path, request: Request) -> Path:
    """Validate both Safari states and seal only the corrected state as acceptance."""
    destination = destination.expanduser().resolve(strict=False)
    if destination.exists():
        raise FileExistsError(destination)

    browser_artifacts = _validate_browser(source_root)
    runtime = _runtime_evidence(request)

    store = EvidenceStore(destination)
    store.write_manifest(
        {
            "schema_version": 1,
            "checkpoint": "native-safari-operator-denial-resolved-discrepancy",
            "created_at": datetime.now(UTC).isoformat(),
            "source_root": source_root.resolve().name,
            "tenant_id": TENANT,
            "workspace_id": None,
            "role": ROLE,
            "route": ROUTE,
            "deployment_ref": DEPLOYMENT,
            "deployment_version": DEPLOYMENT_VERSION,
            "graph_version_ref": GRAPH,
            "provider_calls_performed": 0,
            "mutations_performed": 0,
            "erasure_calls_performed": 0,
            "native_safari_screenshot_count": 2,
            "native_safari_accessibility_snapshot_count": 2,
            "staging_screenshot_media_type": "jpeg",
            "accepted_criteria": list(ACCEPTED_CRITERIA),
        }
    )
    runtime_path = Path("runtime/operator-denial.json")
    store._write_exclusive(runtime_path, runtime)
    evidence_paths = [runtime_path.as_posix()]
    for artifact in browser_artifacts:
        store.ingest_artifact(artifact.source, artifact.destination)
        evidence_paths.append(artifact.destination.as_posix())

    screenshot_index = {
        "schema_version": 1,
        "screenshots": [
            {
                "file": f"screenshots/{BEFORE_STEM}.jpg",
                "accessibility": f"accessibility/{BEFORE_STEM}.txt",
                "route": ROUTE,
                "role": ROLE,
                "evidence_role": "diagnostic_resolved_defect",
                "observation": "denial copy present while mutation controls remained visible",
            },
            {
                "file": f"screenshots/{AFTER_STEM}.jpg",
                "accessibility": f"accessibility/{AFTER_STEM}.txt",
                "route": ROUTE,
                "role": ROLE,
                "evidence_role": "acceptance_corrected_state",
                "observation": "three explicit denials and no retention mutation controls",
            },
        ],
    }
    store._write_exclusive(Path("screenshot-index.json"), screenshot_index)
    evidence_paths.append("screenshot-index.json")
    event_id = store.append_event(
        "campaign.native_safari.operator_denial_verified",
        {
            "result": "pass",
            "tenant_id": TENANT,
            "role": ROLE,
            "route": ROUTE,
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "before_state": "resolved_discrepancy",
            "acceptance_state": "corrected_after",
            "policy_status": 403,
            "legal_holds_status": 403,
            "mutation_call_count": 0,
            "erasure_call_count": 0,
            "provider_call_count": 0,
            "proof_paths": evidence_paths,
        },
        correlation=CorrelationIds(ui_action_id="native-safari-operator-denial-20260825-1"),
    )
    common_evidence = tuple([*evidence_paths, f"events.ndjson#{event_id}"])
    criterion = AcceptanceCriterion(
        ACCEPTED_CRITERIA[0],
        "pass",
        common_evidence,
        note=(
            "Acceptance rests on the corrected after state; the before state is retained "
            "only as resolved-discrepancy evidence."
        ),
    )
    store.finalize_bundle(
        acceptance=(criterion,),
        report_markdown=(
            "# Native Safari operator-denial discrepancy checkpoint\n\n"
            "Native Safari first captured a real operator-scoped defect: the Retention "
            "page displayed authorization-denial copy while Place hold and erasure "
            "controls remained visible. That before image is retained as resolved "
            "discrepancy context and is not acceptance evidence by itself.\n\n"
            "Acceptance rests on the corrected after state. Safari shows the same "
            f"`{ROLE}` scope and served Workflow 1 deployment `{DEPLOYMENT}`, all three "
            "Retention cards display the explicit denial, and no Place, Stage, or Release "
            "control remains. Fresh read-only runtime checks independently prove the exact "
            f"served graph `{GRAPH}`, the singleton operator role, and HTTP 403 from both "
            "retention-policy and legal-hold GETs. The checkpoint made no retention, "
            "erasure, provider, or other runtime mutation. Both browser states were "
            "media-validated, credential-scanned, and checksum sealed.\n"
        ),
    )
    return destination


def _request(path: str, *, method: str = "GET") -> RuntimeResponse:
    """Perform one primary-tenant operator read without exposing its credential."""
    if method != "GET":
        raise RuntimeError("operator-denial checkpoint permits GET requests only")
    response = _scoped_request("primary", path, tenant_id=TENANT, role=ROLE)
    return RuntimeResponse(response.status_code, response.body)


def main() -> int:
    root = build_checkpoint(source_root=SOURCE_ROOT, destination=ROOT, request=_request)
    print(json.dumps({"root": str(root), "sealed": EvidenceStore(root).is_sealed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
