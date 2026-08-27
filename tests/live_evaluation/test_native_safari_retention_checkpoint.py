from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore


def _module():
    return importlib.import_module("release.live_evaluation.native_safari_retention_checkpoint")


def _source(tmp_path: Path, *, invalid_error: bool = True) -> Path:
    source = tmp_path / "source"
    screenshots = source / "screenshots"
    accessibility = source / "accessibility"
    screenshots.mkdir(parents=True)
    accessibility.mkdir(parents=True)
    names = (
        "01-invalid-ttl-native-safari",
        "02-restored-before-refresh-native-safari",
        "03-restored-after-refresh-native-safari",
    )
    for name in names:
        (screenshots / f"{name}.jpg").write_bytes(b"\xff\xd8\xffsafe-jpeg")

    invalid = (
        "URL: 127.0.0.1:3000/console/retention/\n"
        "text evaluation-studio-v1 / tenant-wide / Retention\n"
        "text field Run payloads TTL in days, Value: -1\n"
        "text invalid\n"
        "button (disabled) Save policy\n"
    )
    if invalid_error:
        invalid += "text TTL must be greater than zero days (blank = no expiry).\n"
    (accessibility / f"{names[0]}.txt").write_text(invalid, encoding="utf-8")

    restored = (
        "URL: 127.0.0.1:3000/console/retention/\n"
        "text evaluation-studio-v1 / tenant-wide / Retention\n"
        "button http://127.0.0.1:8122\n"
        "text Retention & Compliance\n"
        "text tenant evaluation-studio-v1\n"
        "checkbox Retention enforcement enabled, Value: 1\n"
        "text no expiry\ntext no expiry\n"
        "button (disabled) Save policy\n"
        "text 8d452480319d4578895007cc8a36c8f0\n"
        "text run 379e3364e2184e93abef39db8cbd3d44\n"
        "text TTLs suspended\n"
    )
    for name in names[1:]:
        (accessibility / f"{name}.txt").write_text(restored, encoding="utf-8")
    return source


def _request(path: str, *, method: str = "GET") -> object:
    assert method == "GET"
    values = {
        "/health": {
            "status": "ok",
            "campaign_id": "evaluation-studio-v1",
            "deployment_ref": "evaluation-studio-v1-grounded-researcher-v1",
            "deployment_version": 6,
            "graph_version_ref": "evaluation-studio-v1-grounded-researcher@4",
        },
        "/v1/identity": {
            "subject": "evaluation-a-platform-admin",
            "tenant_id": "evaluation-studio-v1",
            "workspace_id": None,
            "roles": ["platform_admin"],
        },
        "/v1/retention/policy": {
            "tenant_id": "evaluation-studio-v1",
            "enabled": True,
            "run_ttl_seconds": None,
            "audit_ttl_seconds": None,
        },
        "/v1/retention/legal-holds": [
            {
                "hold_id": "8d452480319d4578895007cc8a36c8f0",
                "tenant_id": "evaluation-studio-v1",
                "run_id": "379e3364e2184e93abef39db8cbd3d44",
                "reason": "[SYNTHETIC DEMO] preserved run",
                "placed_by": "evaluation-operator",
                "active": True,
            }
        ],
    }
    return values[path]


def test_checkpoint_seals_native_safari_retention_validation(tmp_path: Path) -> None:
    module = _module()
    destination = tmp_path / "sealed"

    result = module.build_checkpoint(
        source_root=_source(tmp_path),
        destination=destination,
        request=_request,
    )

    assert result == destination
    assert EvidenceStore(destination).is_sealed
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert [row["criterion_id"] for row in acceptance["criteria"]] == list(module.ACCEPTED_CRITERIA)
    assert all(row["status"] == "pass" for row in acceptance["criteria"])
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["provider_calls_performed"] == 0
    assert manifest["native_safari_screenshot_count"] == 3
    assert manifest["tenant_id"] == "evaluation-studio-v1"
    assert (destination / "runtime/retention-policy.json").is_file()
    assert (destination / "screenshots/03-restored-after-refresh-native-safari.jpg").is_file()


def test_checkpoint_rejects_invalid_ttl_without_associated_error(tmp_path: Path) -> None:
    module = _module()

    with pytest.raises(RuntimeError, match="invalid TTL evidence"):
        module.build_checkpoint(
            source_root=_source(tmp_path, invalid_error=False),
            destination=tmp_path / "bad",
            request=_request,
        )


def test_checkpoint_rejects_runtime_tenant_drift(tmp_path: Path) -> None:
    module = _module()

    def wrong_tenant(path: str, *, method: str = "GET") -> object:
        value = _request(path, method=method)
        if path == "/v1/identity":
            return {**value, "tenant_id": "other-tenant"}  # type: ignore[arg-type]
        return value

    with pytest.raises(RuntimeError, match="identity tenant and role"):
        module.build_checkpoint(
            source_root=_source(tmp_path),
            destination=tmp_path / "bad",
            request=wrong_tenant,
        )
