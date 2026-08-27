from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from release.live_evaluation.browser_refresh import BoundedRefreshEvidenceProducer
from release.live_evaluation.coordinator import ActionRecorder
from release.live_evaluation.evidence import EvidenceStore


def _attachment(name: str, payload: object) -> dict[str, object]:
    return {
        "name": name,
        "body": base64.b64encode(json.dumps(payload).encode()).decode(),
        "contentType": "application/json",
    }


def test_refresh_producer_uses_fixed_playwright_argv_and_exact_report_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = tmp_path / "frontend"
    spec = frontend / "e2e" / "negative-resilience.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("// trusted fixed spec", encoding="utf-8")
    captured: dict[str, object] = {}
    report = {
        "suites": [
            {
                "specs": [
                    {
                        "title": "w2_refresh_restoration has exact fail-closed evidence",
                        "tests": [
                            {
                                "results": [
                                    {
                                        "status": "passed",
                                        "attachments": [
                                            _attachment(
                                                "scenario-verification",
                                                {
                                                    "scenario_id": "w2_refresh_restoration",
                                                    "identity": {
                                                        "run_id": ["run-refresh"],
                                                        "audit_event_id": ["audit-refresh"],
                                                    },
                                                },
                                            ),
                                            _attachment(
                                                "keyboard-focus-order",
                                                {
                                                    "entries": [
                                                        {
                                                            "tag": "button",
                                                            "focus_visible": True,
                                                        }
                                                    ]
                                                },
                                            ),
                                            _attachment(
                                                "refresh-restoration",
                                                {
                                                    "before": {
                                                        "run_id": "run-refresh",
                                                        "ui_run_id": "run-refresh",
                                                        "state": "observed",
                                                    },
                                                    "after": {
                                                        "run_id": "run-refresh",
                                                        "ui_run_id": "run-refresh",
                                                        "state": "observed",
                                                    },
                                                },
                                            ),
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ]
    }

    def run(argv, **kwargs):
        captured.update({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, json.dumps(report), "")

    monkeypatch.setattr(subprocess, "run", run)
    producer = BoundedRefreshEvidenceProducer(
        frontend_root=frontend,
        environment={
            "ZEROTH_EVALUATION_API_KEY": "secret-service-key",
            "ZEROTH_EVALUATION_FAULT_CONTROLLER_KEY": "secret-controller-key",
            "UNRELATED_SECRET": "excluded",
        },
    )

    result = producer.run(
        "w2_refresh_restoration",
        recorder=ActionRecorder(
            EvidenceStore(tmp_path / "evidence"),
            step_id="browser-refresh",
            command_sequence=1,
        ),
    )

    assert captured["argv"] == (
        "npm",
        "exec",
        "playwright",
        "test",
        "e2e/negative-resilience.spec.ts",
        "--project=desktop-1440",
        "--grep",
        "^w2_refresh_restoration has exact fail-closed evidence$",
        "--reporter=json",
    )
    assert "UNRELATED_SECRET" not in captured["env"]
    assert result.run_id == "run-refresh"
    assert result.before_refresh_run_id == "run-refresh"
    assert result.restored_run_id == "run-refresh"
    assert result.correlation["audit_event_id"] == "audit-refresh"
    assert result.keyboard_focus[0]["focus_visible"] is True


def test_refresh_producer_rejects_a_single_relabelled_post_refresh_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = tmp_path / "frontend"
    spec = frontend / "e2e" / "negative-resilience.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("// trusted fixed spec", encoding="utf-8")
    report = {
        "title": "w2_refresh_restoration has exact fail-closed evidence",
        "results": [
            {
                "status": "passed",
                "attachments": [
                    _attachment(
                        "scenario-verification",
                        {
                            "scenario_id": "w2_refresh_restoration",
                            "identity": {"run_id": "run-refresh"},
                        },
                    ),
                    _attachment(
                        "keyboard-focus-order",
                        {"entries": [{"tag": "button", "focus_visible": True}]},
                    ),
                    _attachment(
                        "refresh-restoration",
                        {
                            "before": {
                                "run_id": "run-before",
                                "ui_run_id": "run-before",
                            },
                            "after": {
                                "run_id": "run-refresh",
                                "ui_run_id": "run-refresh",
                            },
                        },
                    ),
                ],
            }
        ],
    }

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(report), ""
        ),
    )
    producer = BoundedRefreshEvidenceProducer(
        frontend_root=frontend,
        environment={"ZEROTH_EVALUATION_API_KEY": "secret-service-key"},
    )

    with pytest.raises(RuntimeError, match="run identity was not restored"):
        producer.run(
            "w2_refresh_restoration",
            recorder=ActionRecorder(
                EvidenceStore(tmp_path / "evidence"),
                step_id="browser-refresh",
                command_sequence=1,
            ),
        )


def test_approval_refresh_requires_the_same_ui_run_and_pending_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frontend = tmp_path / "frontend"
    spec = frontend / "e2e" / "negative-resilience.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("// trusted fixed spec", encoding="utf-8")
    report = {
        "title": "w3_refresh_before_approval has exact fail-closed evidence",
        "results": [
            {
                "status": "passed",
                "attachments": [
                    _attachment(
                        "scenario-verification",
                        {
                            "scenario_id": "w3_refresh_before_approval",
                            "identity": {"run_id": ["run-approval"]},
                        },
                    ),
                    _attachment(
                        "keyboard-focus-order",
                        {"entries": [{"tag": "button", "focus_visible": True}]},
                    ),
                    _attachment(
                        "refresh-restoration",
                        {
                            "before": {
                                "run_id": "run-approval",
                                "ui_run_id": "run-approval",
                                "approval_id": "approval-1",
                                "approval_state": "pending",
                            },
                            "after": {
                                "run_id": "run-approval",
                                "ui_run_id": "run-approval",
                                "approval_id": "approval-1",
                                "approval_state": "pending",
                            },
                        },
                    ),
                ],
            }
        ],
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(report), ""
        ),
    )
    producer = BoundedRefreshEvidenceProducer(
        frontend_root=frontend,
        environment={"ZEROTH_EVALUATION_API_KEY": "secret-service-key"},
    )

    result = producer.run(
        "w3_refresh_before_approval",
        recorder=ActionRecorder(
            EvidenceStore(tmp_path / "evidence"),
            step_id="approval-refresh",
            command_sequence=1,
        ),
    )

    assert result.before_refresh_run_id == "run-approval"
    assert result.restored_run_id == "run-approval"
    assert result.approval_id_before == "approval-1"
    assert result.approval_id_after == "approval-1"
    assert result.approval_state_before == "pending"
    assert result.approval_state_after == "pending"
