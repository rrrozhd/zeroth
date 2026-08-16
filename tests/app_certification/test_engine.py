from __future__ import annotations
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from release.app_certification import (
    MANDATORY_CHECKS,
    AppDeclaration,
    CandidateIdentity,
    CertificationReport,
    CertificationRunner,
    CommandResult,
    HttpResult,
    bind_sbom,
    execute_command,
    validate_report,
    write_report,
    write_provenance,
)
from release.app_certification.checks import run_owned_check
from release.app_certification import checks as owned_checks
from release.app_certification.cli import _untrusted_executor

COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64
SOURCE_DIGEST = "sha256:" + "c" * 64


def declaration_data() -> dict:
    return {
        "schema_version": 2,
        "app_name": "reference-app",
        "zeroth_version": "0.23.9.3",
        "lock_path": "uv.lock",
        "dockerfile": "Dockerfile.certification",
        "image_reference": "reference-app:certification",
        "sbom_path": "evidence/app.spdx.json",
        "provenance_path": "evidence/provenance.json",
        "targets": {
            "graph_builders": ["apps.vendor_dd.graphs:build_main_graph"],
            "contracts": "apps.vendor_dd.contracts:CONTRACTS",
            "auth_config": "apps.vendor_dd.entrypoint:build_auth_config",
            "policy_guard": "apps.vendor_dd.entrypoint:build_policy_guard",
            "frontend_path": "frontend",
        },
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
    sbom = evidence / "app.spdx.json"
    provenance = evidence / "provenance.json"
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n')
    candidate = CandidateIdentity(
        app_name="reference-app",
        app_commit=COMMIT,
        zeroth_version="0.23.9.3",
        image_reference="reference-app:certification",
        image_digest=DIGEST,
        source_digest=SOURCE_DIGEST,
    )
    bind_sbom(sbom, candidate)
    write_provenance(provenance, candidate)


def passing_executor(argv: list[str], cwd: Path) -> CommandResult:
    check = argv[argv.index("--root") - 1]
    structured = json.dumps({"check": check, "schema_version": 1, "status": "passed"})
    return CommandResult(returncode=0, stdout=structured + "\n", stderr="")


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
    return runner.run(expected_commit=COMMIT, image_digest=digest, source_digest=SOURCE_DIGEST)


def test_reference_declaration_produces_bound_deterministic_evidence(tmp_path: Path) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())
    report = run_certification(tmp_path, declaration)
    assert report.status == "passed"
    assert [check.name for check in report.checks] == list(MANDATORY_CHECKS)
    assert all(check.status == "passed" for check in report.checks)
    assert report.candidate is not None
    assert report.candidate.app_commit == COMMIT
    assert report.candidate.zeroth_version == "0.23.9.3"
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


@pytest.mark.parametrize(
    "failed_check",
    [
        "graph",
        "service-config",
        "contracts",
        "dependency-lock",
        "optional-extras",
        "migrations",
        "container-startup",
        "health",
        "policies",
        "frontend-api",
    ],
)
def test_failure_injection_rejects_each_mandatory_check(tmp_path: Path, failed_check: str) -> None:
    write_inputs(tmp_path)
    declaration = AppDeclaration.model_validate(declaration_data())

    def executor(argv: list[str], cwd: Path) -> CommandResult:
        if argv[argv.index("--root") - 1] == failed_check:
            return CommandResult(returncode=23, stdout="", stderr="deliberate failure")
        return passing_executor(argv, cwd)

    report = run_certification(tmp_path, declaration, executor=executor)
    failed = [check for check in report.checks if check.status == "failed"]
    assert report.status == "failed"
    assert [check.name for check in failed] == [failed_check]
    assert failed_check in failed[0].detail
    assert "exited 23" in failed[0].detail


@pytest.mark.parametrize(
    ("name", "field", "target", "diagnostic"),
    [
        (
            "graph",
            "graph_builders",
            ["tests.app_certification.semantic_fixtures:invalid_graph"],
            "contract",
        ),
        (
            "contracts",
            "contracts",
            "tests.app_certification.semantic_fixtures:INVALID_CONTRACTS",
            "Pydantic",
        ),
        (
            "service-config",
            "auth_config",
            "tests.app_certification.semantic_fixtures:invalid_auth_config",
            "ServiceAuthConfig",
        ),
        (
            "policies",
            "policy_guard",
            "tests.app_certification.semantic_fixtures:empty_policy_guard",
            "policy",
        ),
    ],
)
def test_failure_injection_rejects_invalid_semantic_objects(
    tmp_path: Path, name: str, field: str, target, diagnostic: str
) -> None:
    data = declaration_data()
    data["targets"][field] = target
    declaration = AppDeclaration.model_validate(data)

    with pytest.raises(ValueError, match=diagnostic):
        run_owned_check(name, tmp_path, declaration)


def test_failure_injection_rejects_incompatible_installed_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = AppDeclaration.model_validate(declaration_data())
    app_venv = tmp_path / ".venv"
    app_venv.mkdir()
    metadata = tmp_path / "zeroth_core-0.0.0.dist-info/METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Metadata-Version: 2.1\nName: zeroth-core\nVersion: 0.0.0\n")
    monkeypatch.setattr(owned_checks.sys, "prefix", str(app_venv))

    with pytest.raises(ValueError, match="installed Zeroth '0.0.0' does not match"):
        run_owned_check("optional-extras", tmp_path, declaration)


def test_failure_injection_propagates_broken_migration(
    tmp_path: Path, monkeypatch
) -> None:
    declaration = AppDeclaration.model_validate(declaration_data())

    def broken_migration(url: str) -> None:
        raise RuntimeError(f"broken migration at {url}")

    monkeypatch.setattr(owned_checks, "run_migrations", broken_migration)
    with pytest.raises(RuntimeError, match="broken migration"):
        run_owned_check("migrations", tmp_path, declaration)


def test_argv_executor_never_uses_an_ambient_shell(tmp_path: Path, monkeypatch) -> None:
    observed: dict = {}

    class Process:
        returncode = 0
        pid = 123

        def communicate(self, timeout):
            observed["timeout"] = timeout
            return "out", ""

    def fake_popen(argv, **kwargs):
        observed.update(argv=argv, kwargs=kwargs)
        return Process()

    monkeypatch.setattr("release.app_certification.runner.subprocess.Popen", fake_popen)
    result = execute_command(["python", "-c", "print('safe')"], tmp_path)
    assert result.returncode == 0
    assert observed["argv"] == ["python", "-c", "print('safe')"]
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["start_new_session"] is True
    assert observed["timeout"] == 180


def test_untrusted_executor_scrubs_workflow_control_environment(
    tmp_path: Path, monkeypatch
) -> None:
    observed: dict = {}
    monkeypatch.setenv("GITHUB_OUTPUT", "/candidate-must-not-see")

    def capture(argv: list[str], cwd: Path) -> CommandResult:
        observed.update(argv=argv, cwd=cwd)
        return CommandResult(0, "", "")

    monkeypatch.setattr("release.app_certification.cli.execute_command", capture)
    result = _untrusted_executor("app-cert-candidate")(["python", "owned.py"], tmp_path)

    assert result.returncode == 0
    assert observed["argv"][:6] == [
        "sudo",
        "--non-interactive",
        "--user",
        "app-cert-candidate",
        "--",
        "env",
    ]
    assert "-i" in observed["argv"]
    assert not any("GITHUB_OUTPUT" in item for item in observed["argv"])
    assert not any("PYTHONPATH=" in item for item in observed["argv"])
    assert observed["argv"][-2:] == ["python", "owned.py"]


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


def test_failure_injection_missing_lock_fails_dependency_check(tmp_path: Path) -> None:
    declaration = AppDeclaration.model_validate(declaration_data())
    with pytest.raises(ValueError, match="dependency lock.*missing"):
        run_owned_check("dependency-lock", tmp_path, declaration)


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


def test_declaration_rejects_candidate_authored_checks() -> None:
    data = declaration_data()
    data["checks"] = {name: ["true"] for name in MANDATORY_CHECKS}
    with pytest.raises(ValidationError, match="checks"):
        AppDeclaration.model_validate(data)
