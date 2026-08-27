from __future__ import annotations

import base64
import importlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from release.live_evaluation.evidence import EvidenceStore


def _module():
    return importlib.import_module("release.live_evaluation.retention_visual_matrix_checkpoint")


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _network() -> dict[str, list[dict[str, object]]]:
    urls = (
        "http://127.0.0.1:3000/console/retention/",
        "http://127.0.0.1:8122/health",
        "http://127.0.0.1:8122/v1/identity",
        "http://127.0.0.1:8122/v1/retention/policy",
        "http://127.0.0.1:8122/v1/retention/legal-holds",
    )
    return {
        "requests": [
            {
                "method": "GET",
                "resource_type": "document" if index == 0 else "fetch",
                "url": url,
            }
            for index, url in enumerate(urls)
        ],
        "responses": [
            {
                "resource_type": "document" if index == 0 else "fetch",
                "status": 200,
                "url": url,
            }
            for index, url in enumerate(urls)
        ],
    }


def _source(tmp_path: Path) -> Path:
    module = _module()
    root = tmp_path / "source"
    indexed = root / "indexed"
    report = root / "html-report"
    report_data = report / "data"
    indexed.mkdir(parents=True)
    report_data.mkdir(parents=True)

    artifacts: list[dict[str, str]] = []
    project_evidence: dict[str, list[str]] = {}
    report_tests: list[dict[str, Any]] = []
    detail_tests: list[dict[str, Any]] = []
    for project in module.PROJECTS:
        evidence: list[str] = []
        json_attachments = {
            "network-summary": _network(),
            "console-summary": [
                {
                    "type": "log",
                    "message_bytes": 4,
                    "message_sha256": "a" * 64,
                    "url": None,
                }
            ],
            "keyboard-results": [
                {"tag": "input", "role": None, "focus_visible": True},
                *[
                    {"tag": "a", "role": None, "focus_visible": index % 2 == 0}
                    for index in range(7)
                ],
            ],
            "axe-results": [],
        }
        categories = {
            "network-summary": "network",
            "console-summary": "console",
            "keyboard-results": "console",
            "axe-results": "accessibility",
        }
        report_screenshot = f"data/{project}-screenshot.png"
        (report / report_screenshot).write_bytes(b"\x89PNG\r\n\x1a\nsafe")
        attachments: list[dict[str, str]] = [
            {"name": "screenshot", "contentType": "image/png", "path": report_screenshot}
        ]
        for name, value in json_attachments.items():
            filename = f"{project}-{name}.json"
            _write_json(indexed / filename, value)
            destination = f"{categories[name]}/{filename}"
            artifacts.append({"source": f"indexed/{filename}", "destination": destination})
            evidence.append(destination)
            attachments.append(
                {
                    "name": name,
                    "contentType": "application/json",
                    "body": json.dumps(value),
                }
            )

        screenshot = f"{project}-screenshot.png"
        (indexed / screenshot).write_bytes(b"\x89PNG\r\n\x1a\nsafe")
        screenshot_destination = f"screenshots/{screenshot}"
        artifacts.append({"source": f"indexed/{screenshot}", "destination": screenshot_destination})
        evidence.append(screenshot_destination)
        if project == "desktop-1440":
            zoom = f"{project}-zoom-200-screenshot.png"
            (indexed / zoom).write_bytes(b"\x89PNG\r\n\x1a\nsafe")
            zoom_destination = f"screenshots/{zoom}"
            artifacts.append({"source": f"indexed/{zoom}", "destination": zoom_destination})
            evidence.append(zoom_destination)
            attachments.append(
                {
                    "name": "zoom-200-screenshot",
                    "contentType": "image/png",
                    "path": f"data/{project}-zoom.png",
                }
            )
            (report / f"data/{project}-zoom.png").write_bytes(b"\x89PNG\r\n\x1a\nsafe")
        for index in range(2):
            video = f"{project}-video-{index}.webm"
            (indexed / video).write_bytes(b"\x1aE\xdf\xa3safe")
            destination = f"videos/{video}"
            artifacts.append({"source": f"indexed/{video}", "destination": destination})
            evidence.append(destination)
            report_video = f"data/{project}-video-{index}.webm"
            (report / report_video).write_bytes(b"\x1aE\xdf\xa3safe")
            attachments.append({"name": "video", "contentType": "video/webm", "path": report_video})
        project_evidence[project] = evidence
        criteria = [
            module.PROJECT_CRITERIA[project],
            *module.COMMON_CRITERIA,
        ]
        if project == "desktop-1440":
            criteria.extend(("ui.zoom-200-percent", "ui.no-document-overflow"))
        test_id = f"studio-{project}"
        report_tests.append(
            {
                "testId": test_id,
                "title": module.TEST_TITLE,
                "projectName": project,
                "location": {"file": module.TEST_FILE, "line": 68, "column": 7},
                "duration": 1,
                "annotations": [
                    {"type": "criterion", "description": criterion} for criterion in criteria
                ],
                "tags": [],
                "outcome": "expected",
                "path": [],
                "ok": True,
                "results": [{"attachments": attachments}],
            }
        )
        detail_tests.append(
            {
                "testId": test_id,
                "title": module.TEST_TITLE,
                "projectName": project,
                "results": [
                    {
                        "attachments": attachments,
                        "steps": [
                            {"title": f'Navigate to "{module.ROUTE}"', "steps": []},
                        ],
                    }
                ],
            }
        )

    report_json = {
        "metadata": {"actualWorkers": 1},
        "files": [
            {
                "fileId": "studio",
                "fileName": module.TEST_FILE,
                "tests": report_tests,
                "stats": {
                    "total": 5,
                    "expected": 5,
                    "unexpected": 0,
                    "flaky": 0,
                    "skipped": 0,
                    "ok": True,
                },
            }
        ],
        "projectNames": list(module.PROJECTS),
        "stats": {
            "total": 5,
            "expected": 5,
            "unexpected": 0,
            "flaky": 0,
            "skipped": 0,
            "ok": True,
        },
        "errors": [],
    }
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", json.dumps(report_json))
        archive.writestr("studio.json", json.dumps({"tests": detail_tests}))
    report_html = (
        "<!doctype html><title>Playwright Test Report</title>"
        '<template id="playwrightReportBase64">'
        + base64.b64encode(archive_buffer.getvalue()).decode()
        + "</template>"
    )
    (report / "index.html").write_text(report_html, encoding="utf-8")
    artifacts.append(
        {
            "source": "html-report/index.html",
            "destination": "playwright-report/index.html",
        }
    )

    all_evidence = [item for evidence in project_evidence.values() for item in evidence]
    project_ids = {project: f"studio-{project}" for project in module.PROJECTS}
    criteria_rows = []
    for criterion in module.ACCEPTED_CRITERIA:
        if criterion in module.COMMON_CRITERIA:
            projects = module.PROJECTS
        elif criterion in {"ui.zoom-200-percent", "ui.no-document-overflow"}:
            projects = ("desktop-1440",)
        else:
            projects = tuple(
                project
                for project, project_criterion in module.PROJECT_CRITERIA.items()
                if project_criterion == criterion
            )
        evidence = (
            all_evidence
            if projects == module.PROJECTS
            else [item for project in projects for item in project_evidence[project]]
        )
        criteria_rows.append(
            {
                "criterion_id": criterion,
                "status": "pass",
                "test_id": ",".join(project_ids[project] for project in projects),
                "evidence": evidence,
            }
        )
    _write_json(
        root / "results.json",
        {
            "schema_version": 1,
            "completed": True,
            "criteria": criteria_rows,
            "artifacts": artifacts,
        },
    )
    return root


class _Request:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.policy_enabled = True

    def __call__(self, path: str) -> object:
        module = _module()
        self.calls.append(path)
        if path == "/health":
            return {
                "status": "ok",
                "campaign_id": module.TENANT,
                "deployment_ref": module.DEPLOYMENT,
                "deployment_version": 6,
                "graph_version_ref": module.GRAPH,
                "api_key": "must-not-survive",
            }
        if path == "/v1/identity":
            return {
                "subject": "evaluation-service",
                "tenant_id": module.TENANT,
                "workspace_id": None,
                "roles": [module.ROLE],
                "api_key": "must-not-survive",
            }
        if path == "/v1/retention/policy":
            return {
                "tenant_id": module.TENANT,
                "enabled": self.policy_enabled,
                "run_ttl_seconds": None,
                "audit_ttl_seconds": None,
                "api_key": "must-not-survive",
            }
        if path == "/v1/retention/legal-holds":
            return [
                {
                    "hold_id": module.HOLD_ID,
                    "tenant_id": module.TENANT,
                    "run_id": module.HELD_RUN_ID,
                    "reason": "preserve evaluation artifacts",
                    "placed_by": "evaluation-operator",
                    "active": True,
                    "api_key": "must-not-survive",
                }
            ]
        raise AssertionError(path)


def test_checkpoint_seals_exact_retention_visual_matrix(tmp_path: Path) -> None:
    module = _module()
    destination = tmp_path / "sealed"
    request = _Request()

    result = module.build_checkpoint(
        source_root=_source(tmp_path), destination=destination, request=request
    )

    assert result == destination
    assert EvidenceStore(destination).is_sealed
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["projects"] == list(module.PROJECTS)
    assert manifest["screenshot_count"] == 6
    assert manifest["video_count"] == 10
    assert manifest["network_attachment_count"] == 5
    assert manifest["console_attachment_count"] == 5
    assert manifest["keyboard_attachment_count"] == 5
    assert manifest["axe_attachment_count"] == 5
    assert manifest["provider_calls_performed"] == 0
    assert manifest["mutations_performed"] == 0
    assert request.calls == list(module.RUNTIME_PATHS)
    assert "must-not-survive" not in "".join(
        path.read_text(errors="ignore") for path in destination.rglob("*") if path.is_file()
    )


def test_checkpoint_rejects_wrong_project_before_destination_creation(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    results_path = source / "results.json"
    results = json.loads(results_path.read_text())
    results["criteria"][0]["test_id"] = results["criteria"][0]["test_id"].replace(
        "studio-desktop-1440", "other-project"
    )
    _write_json(results_path, results)
    destination = tmp_path / "bad"

    with pytest.raises(RuntimeError, match="Playwright report"):
        module.build_checkpoint(source_root=source, destination=destination, request=_Request())

    assert not destination.exists()


def test_checkpoint_rejects_failed_or_mutating_network_evidence(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    network = next((source / "indexed").glob("*-network-summary.json"))
    value = json.loads(network.read_text())
    value["requests"][0]["method"] = "POST"
    value["responses"][0]["status"] = 500
    _write_json(network, value)

    with pytest.raises(RuntimeError, match="network evidence"):
        module.build_checkpoint(
            source_root=source, destination=tmp_path / "bad", request=_Request()
        )


def test_checkpoint_rejects_incomplete_html_report_data(tmp_path: Path) -> None:
    module = _module()
    source = _source(tmp_path)
    next((source / "html-report/data").glob("*.webm")).unlink()

    with pytest.raises(RuntimeError, match="Playwright report file inventory"):
        module.build_checkpoint(
            source_root=source, destination=tmp_path / "bad", request=_Request()
        )


@pytest.mark.parametrize(
    ("suffix", "value", "message"),
    [
        ("axe-results.json", [{"id": "color-contrast"}], "axe violations"),
        (
            "console-summary.json",
            [{"type": "error", "message_bytes": 1, "message_sha256": "a" * 64, "url": None}],
            "console errors",
        ),
        (
            "keyboard-results.json",
            [{"tag": "a", "role": None, "focus_visible": False}] * 8,
            "focus-visible",
        ),
    ],
)
def test_checkpoint_rejects_accessibility_or_console_failures(
    tmp_path: Path, suffix: str, value: object, message: str
) -> None:
    module = _module()
    source = _source(tmp_path)
    path = next((source / "indexed").glob(f"*-{suffix}"))
    _write_json(path, value)

    with pytest.raises(RuntimeError, match=message):
        module.build_checkpoint(
            source_root=source, destination=tmp_path / "bad", request=_Request()
        )


def test_checkpoint_rejects_runtime_drift_before_destination_creation(tmp_path: Path) -> None:
    module = _module()
    request = _Request()
    request.policy_enabled = False
    destination = tmp_path / "bad"

    with pytest.raises(RuntimeError, match="retention policy"):
        module.build_checkpoint(
            source_root=_source(tmp_path), destination=destination, request=request
        )

    assert not destination.exists()
