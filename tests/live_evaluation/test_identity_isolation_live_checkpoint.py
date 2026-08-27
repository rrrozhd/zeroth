from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from release.live_evaluation.evidence import EvidenceStore


def _module():
    return importlib.import_module("release.live_evaluation.identity_isolation_live_checkpoint")


def test_checkpoint_defaults_to_superseding_clean_source() -> None:
    module = _module()

    assert module.SOURCE_ROOT.name == "identity-isolation-live-20260825-6"
    assert module.ROOT.name == "identity-isolation-live-checkpoint-20260825-1"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _source(tmp_path: Path) -> Path:
    module = _module()
    root = tmp_path / "source"
    indexed = root / "indexed"
    report_data = root / "html-report/data"
    indexed.mkdir(parents=True)
    report_data.mkdir(parents=True)

    artifacts: list[dict[str, str]] = []
    scope_evidence: list[str] = []
    authorization_evidence: list[str] = []
    role_evidence: dict[str, list[str]] = {role: [] for role in module.ROLES}

    for tenant_index, (tenant, _service) in enumerate(module.TENANT_SERVICES.items()):
        for role_index, role in enumerate(module.ROLES):
            stem = f"scope-{tenant_index}-{role_index}-{role}"
            console = f"{stem}.json"
            screenshot = f"{stem}.png"
            video = f"{stem}.webm"
            _write_json(
                indexed / console,
                {
                    "cross_tenant_status": 404,
                    "cross_tenant_legal_holds_payload_fields": ["detail"],
                    "cross_tenant_legal_holds_status": (
                        404 if role in {"admin", "platform_admin"} else 403
                    ),
                    "cross_tenant_retention_payload_fields": ["detail"],
                    "cross_tenant_retention_policy_status": (
                        404 if role in {"admin", "platform_admin"} else 403
                    ),
                    "own_retention_policy_status": (
                        200 if role in {"admin", "platform_admin"} else 403
                    ),
                    "own_service_status": 200,
                    "role": role,
                    "tenant_id": tenant,
                },
            )
            (indexed / screenshot).write_bytes(b"\x89PNG\r\n\x1a\nsafe")
            (indexed / video).write_bytes(b"\x1aE\xdf\xa3safe")
            destinations = (
                f"console/{console}",
                f"screenshots/{screenshot}",
                f"videos/{video}",
            )
            artifacts.extend(
                {
                    "source": f"indexed/{name}",
                    "destination": destination,
                }
                for name, destination in zip(
                    (console, screenshot, video), destinations, strict=True
                )
            )
            scope_evidence.extend(destinations)
            role_evidence[role].extend(destinations)

    for role_index, role in enumerate(module.ROLES):
        stem = f"authorization-{role_index}-{role}"
        console = f"{stem}.json"
        video = f"{stem}.webm"
        expected = module.ROLE_ACCESS[role]
        _write_json(
            indexed / console,
            {
                "tenant_id": module.PRIMARY_TENANT,
                "role": role,
                "audit_allowed": expected["audit"],
                "economics_allowed": expected["economics"],
                "retention_allowed": expected["retention-compliance"],
            },
        )
        (indexed / video).write_bytes(b"\x1aE\xdf\xa3safe")
        console_destination = f"console/{console}"
        video_destination = f"videos/{video}"
        artifacts.extend(
            (
                {"source": f"indexed/{console}", "destination": console_destination},
                {"source": f"indexed/{video}", "destination": video_destination},
            )
        )
        authorization_evidence.extend((console_destination, video_destination))
        role_evidence[role].extend((console_destination, video_destination))
        for surface in module.SURFACES:
            screenshot = f"authorization-{role}-{surface}.png"
            (indexed / screenshot).write_bytes(b"\x89PNG\r\n\x1a\nsafe")
            destination = f"screenshots/{screenshot}"
            artifacts.append({"source": f"indexed/{screenshot}", "destination": destination})
            authorization_evidence.append(destination)
            role_evidence[role].append(destination)

    (root / "html-report/index.html").write_text(
        "<html><body>safe report</body></html>", encoding="utf-8"
    )
    for index in range(20):
        (report_data / f"shot-{index}.png").write_bytes(b"\x89PNG\r\n\x1a\nsafe")
    for index in range(12):
        (report_data / f"video-{index}.webm").write_bytes(b"\x1aE\xdf\xa3safe")
    artifacts.append(
        {
            "source": "html-report/index.html",
            "destination": "playwright-report/index.html",
        }
    )

    criteria = [
        {
            "criterion_id": "identity.authoritative-scope",
            "status": "pass",
            "test_id": "scope-tests",
            "evidence": scope_evidence,
        },
        {
            "criterion_id": "identity.retention-tenant-isolation",
            "status": "pass",
            "test_id": "scope-tests",
            "evidence": scope_evidence,
        },
        {
            "criterion_id": "identity.role-denial",
            "status": "pass",
            "test_id": "authorization-tests",
            "evidence": authorization_evidence,
        },
        *[
            {
                "criterion_id": f"identity.role.{role}",
                "status": "pass",
                "test_id": f"role-{role}",
                "evidence": role_evidence[role],
            }
            for role in module.ROLES
        ],
        {
            "criterion_id": "identity.tenant-isolation",
            "status": "pass",
            "test_id": "tenant-tests",
            "evidence": scope_evidence,
        },
    ]
    _write_json(
        root / "results.json",
        {
            "schema_version": 1,
            "completed": True,
            "criteria": criteria,
            "artifacts": artifacts,
        },
    )
    return root


class _Request:
    def __init__(self) -> None:
        self.cross_tenant_status = 404
        self.denied_status = 403
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(
        self,
        service: str,
        path: str,
        *,
        tenant_id: str,
        role: str,
    ) -> Any:
        module = _module()
        self.calls.append((service, path, tenant_id, role))
        served_tenant = next(
            tenant
            for tenant, candidate_service in module.TENANT_SERVICES.items()
            if candidate_service == service
        )
        if path == "/health":
            return module.RuntimeResponse(
                200,
                dict(module.EXPECTED_HEALTH[service]),
            )
        if path == "/v1/identity":
            if tenant_id != served_tenant:
                return module.RuntimeResponse(
                    self.cross_tenant_status,
                    {"detail": "not found", "api_key": "must-not-survive"},
                )
            fixture = "a" if tenant_id == module.PRIMARY_TENANT else "b"
            return module.RuntimeResponse(
                200,
                {
                    "subject": f"evaluation-{fixture}-{role.replace('_', '-')}",
                    "tenant_id": tenant_id,
                    "workspace_id": None,
                    "roles": [role],
                    "api_key": "must-not-survive",
                },
            )
        surface = next(name for name, route in module.SURFACE_PATHS.items() if route == path)
        expected = module.ROLE_ACCESS[role][surface]
        return module.RuntimeResponse(200 if expected else self.denied_status, {})


def test_checkpoint_seals_exact_identity_and_role_evidence(tmp_path: Path) -> None:
    module = _module()
    destination = tmp_path / "sealed"
    request = _Request()

    result = module.build_checkpoint(
        source_root=_source(tmp_path),
        destination=destination,
        request=request,
    )

    assert result == destination
    assert EvidenceStore(destination).is_sealed
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert [row["criterion_id"] for row in acceptance["criteria"]] == list(module.ACCEPTED_CRITERIA)
    assert all(row["status"] == "pass" for row in acceptance["criteria"])
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["scope_screenshot_count"] == 8
    assert manifest["role_screenshot_count"] == 12
    assert manifest["video_count"] == 12
    assert manifest["html_report_file_count"] == 33
    assert manifest["provider_calls_performed"] == 0
    assert len(list((destination / "playwright-report/data").iterdir())) == 32
    assert (destination / "runtime/identity-matrix.json").is_file()
    assert (destination / "runtime/authorization-matrix.json").is_file()
    assert "must-not-survive" not in "".join(
        path.read_text(errors="ignore") for path in destination.rglob("*") if path.is_file()
    )
    assert all(len(call) == 4 for call in request.calls)


def test_checkpoint_rejects_nonexact_criterion_set_before_creating_destination(
    tmp_path: Path,
) -> None:
    module = _module()
    source = _source(tmp_path)
    results = json.loads((source / "results.json").read_text())
    results["criteria"].append(
        {
            "criterion_id": "identity.unreviewed",
            "status": "pass",
            "test_id": "invented",
            "evidence": [],
        }
    )
    _write_json(source / "results.json", results)
    destination = tmp_path / "bad"

    with pytest.raises(RuntimeError, match="criteria do not match"):
        module.build_checkpoint(
            source_root=source,
            destination=destination,
            request=_Request(),
        )

    assert not destination.exists()


def test_checkpoint_rejects_incorrect_authorization_attachment(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    path = next((source / "indexed").glob("authorization-*-operator.json"))
    value = json.loads(path.read_text())
    value["audit_allowed"] = True
    _write_json(path, value)

    with pytest.raises(RuntimeError, match="authorization result matrix"):
        module.build_checkpoint(
            source_root=source,
            destination=tmp_path / "bad",
            request=_Request(),
        )


def test_checkpoint_rejects_incomplete_retention_isolation_attachment(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    path = next((source / "indexed").glob("scope-*-operator.json"))
    value = json.loads(path.read_text())
    del value["cross_tenant_legal_holds_status"]
    _write_json(path, value)

    with pytest.raises(RuntimeError, match="scope result matrix"):
        module.build_checkpoint(
            source_root=source,
            destination=tmp_path / "bad",
            request=_Request(),
        )


def test_checkpoint_rejects_incorrect_own_retention_access(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    path = next((source / "indexed").glob("scope-*-reviewer.json"))
    value = json.loads(path.read_text())
    value["own_retention_policy_status"] = 200
    _write_json(path, value)

    with pytest.raises(RuntimeError, match="scope result matrix"):
        module.build_checkpoint(
            source_root=source,
            destination=tmp_path / "bad",
            request=_Request(),
        )


def test_checkpoint_rejects_visible_cross_tenant_retention_route(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    path = next((source / "indexed").glob("scope-*-admin.json"))
    value = json.loads(path.read_text())
    value["cross_tenant_retention_policy_status"] = 200
    _write_json(path, value)

    with pytest.raises(RuntimeError, match="scope result matrix"):
        module.build_checkpoint(
            source_root=source,
            destination=tmp_path / "bad",
            request=_Request(),
        )


def test_checkpoint_rejects_cross_tenant_retention_data_fields(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    path = next((source / "indexed").glob("scope-*-admin.json"))
    value = json.loads(path.read_text())
    value["cross_tenant_legal_holds_payload_fields"] = ["detail", "items"]
    _write_json(path, value)

    with pytest.raises(RuntimeError, match="scope result matrix"):
        module.build_checkpoint(
            source_root=source,
            destination=tmp_path / "bad",
            request=_Request(),
        )


def test_checkpoint_rejects_runtime_cross_tenant_visibility(tmp_path: Path) -> None:
    module = _module()
    request = _Request()
    request.cross_tenant_status = 200

    with pytest.raises(RuntimeError, match="cross-tenant identity request"):
        module.build_checkpoint(
            source_root=_source(tmp_path),
            destination=tmp_path / "bad",
            request=request,
        )


def test_checkpoint_rejects_runtime_role_denial_drift(tmp_path: Path) -> None:
    module = _module()
    request = _Request()
    request.denied_status = 200

    with pytest.raises(RuntimeError, match="runtime authorization matrix"):
        module.build_checkpoint(
            source_root=_source(tmp_path),
            destination=tmp_path / "bad",
            request=request,
        )
