from __future__ import annotations

import json
from pathlib import Path

import pytest

from release.acceptance.cli import main, write_report_atomic
from release.acceptance.models import AcceptanceReport, ScenarioResult, ScenarioStatus


def _report(status: ScenarioStatus) -> AcceptanceReport:
    return AcceptanceReport(
        status=status,
        target_origin="https://candidate.example",
        tenant_id="acceptance-tenant",
        namespace="acceptance-tenant-01234567",
        deployment_ref="dep",
        candidate_digest="sha256:" + "a" * 64,
        image_identity={"candidate": "sha256:" + "b" * 64},
        started_at="2026-08-09T00:00:00Z",
        finished_at="2026-08-09T00:00:01Z",
        scenarios=[ScenarioResult(name="readiness", status=status, detail=status.value)],
        cleanup=[],
    )


def test_atomic_report_write_leaves_no_partial_file(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "report.json"

    write_report_atomic(output, _report(ScenarioStatus.PASSED))

    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "passed"
    assert list(output.parent.glob(".*.tmp")) == []


def test_cli_rejects_bad_configuration_before_transport_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"schema_version":1,"base_url":"file:///unsafe"}', encoding="utf-8")
    created = False

    def transport_factory(_config):
        nonlocal created
        created = True
        raise AssertionError("transport must not be created")

    code = main(
        [
            "--config",
            str(config),
            "--contract",
            str(tmp_path / "missing.json"),
            "--output",
            str(tmp_path / "report.json"),
        ],
        environ={},
        transport_factory=transport_factory,
    )

    assert code == 2
    assert not created
    assert "configuration failed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "expected"),
    [(ScenarioStatus.PASSED, 0), (ScenarioStatus.FAILED, 1)],
)
def test_cli_exit_code_follows_report_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: ScenarioStatus,
    expected: int,
) -> None:
    async def fake_run(*_args, **_kwargs):
        return _report(status)

    monkeypatch.setattr("release.acceptance.cli.run_from_paths", fake_run)
    output = tmp_path / "report.json"

    code = main(
        ["--config", "config.json", "--contract", "contract.json", "--output", str(output)],
        environ={},
    )

    assert code == expected
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == status.value
