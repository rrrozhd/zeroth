from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from release.app_certification import (
    MANDATORY_CHECKS,
    AppDeclaration,
    CertificationReport,
    CertificationRunner,
    CommandResult,
    HttpResult,
    execute_command,
    load_declaration,
    validate_report,
    write_report,
)
from release.app_certification.cli import main as certification_main
from release.app_certification.cli import resolve_smoke_headers

COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64


def declaration_data() -> dict:
    return {
        "schema_version": 1,
        "app_name": "reference-app",
        "zeroth_version": "0.23.8",
        "lock_path": "uv.lock",
        "dockerfile": "Dockerfile.certification",
        "image_reference": "reference-app:certification",
        "sbom_path": "evidence/app.spdx.json",
        "provenance_path": "evidence/provenance.json",
        "checks": {name: ["certify", name] for name in MANDATORY_CHECKS},
        "smoke": {
            "method": "POST",
            "path": "/v1/runs",
            "request_json": {"input_payload": {"case": "fixed"}},
            "expected_status": 202,
            "expected_json": {"status": "accepted", "result": {"case": "fixed"}},
        },
    }


def write_inputs(root: Path) -> None:
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    evidence = root / "evidence"
    evidence.mkdir()
    (evidence / "app.spdx.json").write_text('{"spdxVersion":"SPDX-2.3"}\n')
    (evidence / "provenance.json").write_text('{"predicateType":"slsa"}\n')


def passing_executor(argv: list[str], cwd: Path) -> CommandResult:
    return CommandResult(returncode=0, stdout="ok\n", stderr="")


def passing_http(check: str, smoke) -> HttpResult:
    return HttpResult(
        status_code=202,
        json_body={"status": "accepted", "result": {"case": "fixed", "extra": True}},
    )


def run_certification(
    root: Path,
    declaration: AppDeclaration,
    *,
    executor=passing_executor,
    http=passing_http,
    digest: str = DIGEST,
    measured_commit: str = COMMIT,
) -> CertificationReport:
    runner = CertificationRunner(
        root,
        declaration,
        executor=executor,
        http=http,
        commit_reader=lambda _: measured_commit,
    )
    return runner.run(expected_commit=COMMIT, image_digest=digest)


def test_reference_declaration_produces_bound_deterministic_evidence(tmp_path: Path) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())

    report = run_certification(tmp_path, declaration)

    assert report.status == "passed"
    assert [check.name for check in report.checks] == list(MANDATORY_CHECKS)
    assert all(check.status == "passed" for check in report.checks)
    assert report.candidate is not None
    assert report.candidate.app_commit == COMMIT
    assert report.candidate.zeroth_version == "0.23.8"
    assert report.candidate.image_digest == DIGEST
    assert report.evidence is not None
    assert report.evidence.candidate_identity_digest.startswith("sha256:")
    assert report.evidence.sbom.sha256.startswith("sha256:")
    assert report.evidence.provenance.sha256.startswith("sha256:")

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    write_report(report, first)
    write_report(report, second)
    assert first.read_bytes() == second.read_bytes()
    assert validate_report(first).status == "passed"


@pytest.mark.parametrize("failed_check", MANDATORY_CHECKS)
def test_failure_injection_rejects_each_mandatory_check(
    tmp_path: Path, failed_check: str
) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())

    def executor(argv: list[str], cwd: Path) -> CommandResult:
        if argv[-1] == failed_check:
            return CommandResult(returncode=23, stdout="", stderr="deliberate failure")
        return passing_executor(argv, cwd)

    report = run_certification(tmp_path, declaration, executor=executor)
    failed = [check for check in report.checks if check.status == "failed"]

    assert report.status == "failed"
    assert [check.name for check in failed] == [failed_check]
    assert failed_check in failed[0].detail
    assert "exited 23" in failed[0].detail


def test_argv_executor_never_uses_an_ambient_shell(tmp_path: Path, monkeypatch) -> None:
    observed: dict = {}

    def fake_run(argv, **kwargs):
        observed.update(argv=argv, kwargs=kwargs)

        class Completed:
            returncode = 0
            stdout = "out"
            stderr = ""

        return Completed()

    monkeypatch.setattr("release.app_certification.runner.subprocess.run", fake_run)
    result = execute_command(["python", "-c", "print('safe')"], tmp_path)

    assert result.returncode == 0
    assert observed["argv"] == ["python", "-c", "print('safe')"]
    assert observed["kwargs"]["shell"] is False


@pytest.mark.parametrize(
    ("check_name", "actual"),
    [
        ("packaged-smoke", {"status": "rejected"}),
        ("ephemeral-smoke", {"status": "accepted", "result": {"case": "wrong"}}),
    ],
)
def test_failed_smoke_subset_assertion_is_actionable(
    tmp_path: Path, check_name: str, actual: dict
) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())

    def http(name: str, smoke) -> HttpResult:
        body = actual if name == check_name else passing_http(name, smoke).json_body
        return HttpResult(status_code=202, json_body=body)

    report = run_certification(tmp_path, declaration, http=http)
    failure = next(check for check in report.checks if check.name == check_name)

    assert report.status == "failed"
    assert failure.status == "failed"
    assert "JSON subset mismatch" in failure.detail
    assert "expected" in failure.detail


def test_malformed_http_result_does_not_hide_the_report(tmp_path: Path) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())

    def http(name: str, smoke):
        if name == "packaged-smoke":
            return HttpResult(status_code=202, json_body={"status": object()})
        return passing_http(name, smoke)

    report = run_certification(tmp_path, declaration, http=http)
    failure = next(check for check in report.checks if check.name == "packaged-smoke")

    assert report.status == "failed"
    assert failure.status == "failed"
    assert "malformed HTTP result" in failure.detail


def test_missing_lock_fails_dependency_lock_without_hiding_report(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "app.spdx.json").write_text("sbom")
    (evidence / "provenance.json").write_text("provenance")
    declaration = AppDeclaration.model_validate(declaration_data())

    report = run_certification(tmp_path, declaration)
    failure = next(check for check in report.checks if check.name == "dependency-lock")

    assert report.status == "failed"
    assert failure.status == "failed"
    assert "uv.lock" in failure.detail
    assert "missing" in failure.detail


@pytest.mark.parametrize("lock_path", ["", "/tmp/uv.lock", "../uv.lock", "bad\npath", r"bad\path"])
def test_invalid_lock_path_is_rejected(lock_path: str) -> None:
    data = declaration_data()
    data["lock_path"] = lock_path
    with pytest.raises(ValidationError, match="lock_path"):
        AppDeclaration.model_validate(data)


@pytest.mark.parametrize("reference", ["-bad:tag", "bad reference:tag", "app\nforged=value"])
def test_unsafe_image_reference_is_rejected(reference: str) -> None:
    data = declaration_data()
    data["image_reference"] = reference

    with pytest.raises(ValidationError, match="image_reference"):
        AppDeclaration.model_validate(data)


@pytest.mark.parametrize("pin", ["", "^0.23.8", ">=0.23.8", "0.23.*", "v0.23.8"])
def test_non_exact_zeroth_pin_is_rejected(pin: str) -> None:
    data = declaration_data()
    data["zeroth_version"] = pin
    with pytest.raises(ValidationError, match="zeroth_version"):
        AppDeclaration.model_validate(data)


@pytest.mark.parametrize("digest", ["", "sha256:short", "b" * 64, "sha512:" + "b" * 64])
def test_invalid_daemon_digest_fails_closed_with_a_report(tmp_path: Path, digest: str) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())

    report = run_certification(tmp_path, declaration, digest=digest)

    assert report.status == "failed"
    for name in ("sbom", "provenance"):
        failure = next(check for check in report.checks if check.name == name)
        assert failure.status == "failed"
        assert "image digest" in failure.detail


@pytest.mark.parametrize("kind", ["sbom", "provenance"])
@pytest.mark.parametrize("contents", [None, b""])
def test_missing_or_empty_evidence_fails_its_named_check(
    tmp_path: Path, kind: str, contents: bytes | None
) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())
    path = tmp_path / getattr(declaration, f"{kind}_path")
    path.unlink()
    if contents is not None:
        path.write_bytes(contents)

    report = run_certification(tmp_path, declaration)
    failure = next(check for check in report.checks if check.name == kind)

    assert report.status == "failed"
    assert failure.status == "failed"
    assert kind in failure.detail
    assert "missing" in failure.detail or "empty" in failure.detail


def test_commit_mismatch_fails_identity_bound_evidence_checks(tmp_path: Path) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())

    report = run_certification(tmp_path, declaration, measured_commit="c" * 40)

    assert report.status == "failed"
    assert report.candidate is None
    for name in ("sbom", "provenance"):
        failure = next(check for check in report.checks if check.name == name)
        assert "commit mismatch" in failure.detail


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_declaration_requires_exact_mandatory_check_set(mutation: str) -> None:
    data = declaration_data()
    if mutation == "missing":
        data["checks"].pop("policies")
    else:
        data["checks"]["custom"] = ["certify", "custom"]
    with pytest.raises(ValidationError, match="checks"):
        AppDeclaration.model_validate(data)


def test_unknown_fields_and_empty_command_are_rejected() -> None:
    data = declaration_data()
    data["future_option"] = True
    with pytest.raises(ValidationError, match="future_option"):
        AppDeclaration.model_validate(data)

    data = declaration_data()
    data["checks"]["graph"] = []
    with pytest.raises(ValidationError, match="graph"):
        AppDeclaration.model_validate(data)


def test_json_loader_rejects_duplicate_check_names(tmp_path: Path) -> None:
    data = declaration_data()
    checks = json.dumps(data["checks"])[1:-1]
    raw = json.dumps({key: value for key, value in data.items() if key != "checks"})
    raw = raw[:-1] + f',"checks":{{{checks},"graph":["second"]}}}}'
    path = tmp_path / "certification.json"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key 'graph'"):
        load_declaration(path)


@pytest.mark.parametrize(
    "headers",
    [
        {"Bad Header": "CERT_KEY"},
        {"X-API-Key": "9BAD_ENV"},
        {"Content-Type": "CERT_KEY"},
    ],
)
def test_unsafe_smoke_header_environment_mapping_is_rejected(headers: dict[str, str]) -> None:
    data = declaration_data()
    data["smoke"]["headers_from_env"] = headers

    with pytest.raises(ValidationError, match="smoke"):
        AppDeclaration.model_validate(data)


def test_cli_fails_closed_when_smoke_header_environment_is_missing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    data = declaration_data()
    data["smoke"]["headers_from_env"] = {"X-API-Key": "MISSING_CERTIFICATION_KEY"}
    declaration = tmp_path / "certification.json"
    declaration.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.delenv("MISSING_CERTIFICATION_KEY", raising=False)
    report = tmp_path / "report.json"

    result = certification_main(
        [
            "run",
            "--declaration",
            str(declaration),
            "--root",
            str(tmp_path),
            "--app-commit",
            COMMIT,
            "--image-digest",
            DIGEST,
            "--packaged-url",
            "http://127.0.0.1:18080",
            "--ephemeral-url",
            "http://127.0.0.1:18081",
            "--report",
            str(report),
        ]
    )

    assert result == 2
    assert "MISSING_CERTIFICATION_KEY" in capsys.readouterr().err
    assert not report.exists()


def test_smoke_headers_resolve_only_from_the_named_environment() -> None:
    data = declaration_data()
    data["smoke"]["headers_from_env"] = {"X-API-Key": "CERTIFICATION_KEY"}
    smoke = AppDeclaration.model_validate(data).smoke

    assert resolve_smoke_headers(smoke, {"CERTIFICATION_KEY": "opaque-value"}) == {
        "X-API-Key": "opaque-value"
    }
