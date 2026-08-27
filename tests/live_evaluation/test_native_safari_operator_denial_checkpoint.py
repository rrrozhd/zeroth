from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from release.live_evaluation.evidence import EvidenceStore, UnsafeEvidenceError


def _module():
    return importlib.import_module(
        "release.live_evaluation.native_safari_operator_denial_checkpoint"
    )


def _source(
    tmp_path: Path,
    *,
    before_leak: bool = True,
    after_leak: bool = False,
    after_denial_count: int = 3,
    valid_screenshot: bool = True,
    secret: bool = False,
) -> Path:
    source = tmp_path / "source"
    accessibility = source / "accessibility"
    screenshots = source / "screenshots"
    accessibility.mkdir(parents=True)
    screenshots.mkdir(parents=True)

    denial = (
        "Retention controls are hidden because this API key cannot read "
        "retention control administration data."
    )
    common = (
        "URL: 127.0.0.1:3000/console/retention/\n"
        "container Scope: evaluation-studio-v1 / tenant-wide; roles: operator\n"
        "text evaluation-studio-v1 / tenant-wide / Retention\n"
        "text operator local served:  evaluation-studio-v1-grounded-researcher-v1\n"
        "heading Retention & Compliance\n"
    )
    before = common + f"text Retention policy\ntext {denial}\ntext Legal holds\ntext {denial}\n"
    if before_leak:
        before += (
            "text field Legal hold run ID\n"
            "text field Legal hold reason\n"
            "button Place hold\n"
            "text Erasure requests\n"
            "button scope entire tenant\n"
            "button scope single run\n"
            "text field run_id\n"
            "button (disabled) Stage erasure request\n"
        )
    after = common + "text Retention policy\ntext Legal holds\ntext Erasure requests\n"
    after += "\n".join(f"text {denial}" for _ in range(after_denial_count)) + "\n"
    if after_leak:
        after += "button Place hold\nbutton Stage erasure request\nbutton Release hold\n"
    if secret:
        after += "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"

    (accessibility / "01-operator-retention-controls-leak-before-fix.txt").write_text(
        before, encoding="utf-8"
    )
    (accessibility / "02-operator-retention-controls-hidden-after-fix.txt").write_text(
        after, encoding="utf-8"
    )
    screenshot_bytes = b"\xff\xd8\xffsafe-native-safari-jpeg" if valid_screenshot else b"bad"
    for name in (
        "01-operator-retention-controls-leak-before-fix.png",
        "02-operator-retention-controls-hidden-after-fix.png",
    ):
        (screenshots / name).write_bytes(screenshot_bytes)
    return source


def _request_recorder(module, *, policy_status: int = 403, holds_status: int = 403):
    calls: list[tuple[str, str]] = []

    def request(path: str, *, method: str = "GET"):
        calls.append((method, path))
        responses = {
            "/health": module.RuntimeResponse(
                200,
                {
                    "status": "ok",
                    "campaign_id": "evaluation-studio-v1",
                    "deployment_ref": "evaluation-studio-v1-grounded-researcher-v1",
                    "deployment_version": 6,
                    "graph_version_ref": "evaluation-studio-v1-grounded-researcher@4",
                },
            ),
            "/v1/identity": module.RuntimeResponse(
                200,
                {
                    "subject": "evaluation-a-operator",
                    "tenant_id": "evaluation-studio-v1",
                    "workspace_id": None,
                    "roles": ["operator"],
                },
            ),
            "/v1/retention/policy": module.RuntimeResponse(policy_status, {}),
            "/v1/retention/legal-holds": module.RuntimeResponse(holds_status, {}),
        }
        return responses[path]

    return request, calls


def test_checkpoint_seals_resolved_operator_denial_with_after_state_as_acceptance(
    tmp_path: Path,
) -> None:
    module = _module()
    request, calls = _request_recorder(module)
    destination = tmp_path / "sealed"

    result = module.build_checkpoint(
        source_root=_source(tmp_path), destination=destination, request=request
    )

    assert result == destination
    assert EvidenceStore(destination).is_sealed
    assert calls == [
        ("GET", "/health"),
        ("GET", "/v1/identity"),
        ("GET", "/v1/retention/policy"),
        ("GET", "/v1/retention/legal-holds"),
    ]
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert acceptance["criteria"] == [
        {
            "criterion_id": "product.identity.native-safari-role-denial",
            "status": "pass",
            "evidence": acceptance["criteria"][0]["evidence"],
            "note": "Acceptance rests on the corrected after state; the before state is retained only as resolved-discrepancy evidence.",
        }
    ]
    assert {
        "screenshots/01-operator-retention-controls-leak-before-fix.jpg",
        "screenshots/02-operator-retention-controls-hidden-after-fix.jpg",
        "accessibility/01-operator-retention-controls-leak-before-fix.txt",
        "accessibility/02-operator-retention-controls-hidden-after-fix.txt",
        "runtime/operator-denial.json",
    } <= set(acceptance["criteria"][0]["evidence"])
    runtime = json.loads((destination / "runtime/operator-denial.json").read_text())
    assert runtime["identity"]["roles"] == ["operator"]
    assert runtime["denials"] == {
        "legal_holds_status": 403,
        "policy_status": 403,
    }
    index = json.loads((destination / "screenshot-index.json").read_text())
    assert [row["evidence_role"] for row in index["screenshots"]] == [
        "diagnostic_resolved_defect",
        "acceptance_corrected_state",
    ]
    report = (destination / "report.md").read_text()
    assert "Acceptance rests on the corrected after state" in report
    assert "not acceptance evidence by itself" in report


def test_checkpoint_rejects_after_state_with_any_retention_mutation_control(
    tmp_path: Path,
) -> None:
    module = _module()
    request, _ = _request_recorder(module)

    with pytest.raises(RuntimeError, match="after state exposes retention mutation controls"):
        module.build_checkpoint(
            source_root=_source(tmp_path, after_leak=True),
            destination=tmp_path / "bad",
            request=request,
        )


def test_checkpoint_rejects_before_state_that_does_not_capture_the_discrepancy(
    tmp_path: Path,
) -> None:
    module = _module()
    request, _ = _request_recorder(module)

    with pytest.raises(RuntimeError, match="before state does not prove the control leak"):
        module.build_checkpoint(
            source_root=_source(tmp_path, before_leak=False),
            destination=tmp_path / "bad",
            request=request,
        )


def test_checkpoint_requires_three_explicit_after_denials_and_exact_runtime_403s(
    tmp_path: Path,
) -> None:
    module = _module()
    request, _ = _request_recorder(module)
    with pytest.raises(RuntimeError, match="three explicit denial cards"):
        module.build_checkpoint(
            source_root=_source(tmp_path, after_denial_count=2),
            destination=tmp_path / "bad-denials",
            request=request,
        )

    request, _ = _request_recorder(module, holds_status=200)
    with pytest.raises(RuntimeError, match="policy and legal-hold reads were not both denied"):
        module.build_checkpoint(
            source_root=_source(tmp_path / "status"),
            destination=tmp_path / "bad-status",
            request=request,
        )


def test_checkpoint_rejects_invalid_screenshot_and_secret_bearing_accessibility(
    tmp_path: Path,
) -> None:
    module = _module()
    request, _ = _request_recorder(module)
    with pytest.raises(RuntimeError, match="invalid native Safari screenshot"):
        module.build_checkpoint(
            source_root=_source(tmp_path, valid_screenshot=False),
            destination=tmp_path / "bad-image",
            request=request,
        )

    request, _ = _request_recorder(module)
    with pytest.raises(UnsafeEvidenceError, match="secret-shaped"):
        module.build_checkpoint(
            source_root=_source(tmp_path / "secret", secret=True),
            destination=tmp_path / "bad-secret",
            request=request,
        )
