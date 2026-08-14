from __future__ import annotations

import base64
import io
import json
import subprocess
import tarfile
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from release.app_certification import (
    AppDeclaration,
    CandidateIdentity,
    CertificationReport,
    CertificationTargets,
    bind_sbom,
    execute_command,
    finalize_attestation,
    identity_digest,
    scaffold_checkout,
    validate_image_archive,
    validate_report,
    write_provenance,
    write_report,
)
from release.app_certification.checks import run_owned_check
from release.app_certification import checks as owned_checks
from apps.vendor_dd import certification_healthcheck


COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64


def declaration_data() -> dict:
    return {
        "schema_version": 2,
        "app_name": "reference-app",
        "zeroth_version": "0.23.9.1",
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
            "expected_json": {"status": "accepted"},
        },
    }


def identity() -> CandidateIdentity:
    return CandidateIdentity(
        app_name="reference-app",
        app_commit=COMMIT,
        zeroth_version="0.23.9.1",
        image_reference="reference-app:certification",
        image_digest=DIGEST,
    )


def test_candidate_cannot_replace_owned_checks_with_argv() -> None:
    declaration = AppDeclaration.model_validate(declaration_data())
    assert isinstance(declaration.targets, CertificationTargets)

    hostile = declaration_data()
    hostile["checks"] = {"graph": ["true"]}
    with pytest.raises(ValidationError, match="checks"):
        AppDeclaration.model_validate(hostile)


def test_owned_check_timeout_kills_the_isolated_process_group(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[int, int]] = []

    class Process:
        returncode = None
        pid = 456
        timed_out = False

        def communicate(self, timeout=None):
            if not self.timed_out:
                self.timed_out = True
                raise subprocess.TimeoutExpired(["owned"], timeout)
            return "", ""

    monkeypatch.setattr(
        "release.app_certification.runner.subprocess.Popen", lambda *a, **k: Process()
    )
    monkeypatch.setattr(
        "release.app_certification.runner.os.killpg", lambda pid, sig: calls.append((pid, sig))
    )

    result = execute_command(["owned"], tmp_path)

    assert result.returncode == 124
    assert calls and calls[0][0] == 456


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
def test_owned_semantic_checks_reject_invalid_app_objects(
    tmp_path: Path, name: str, field: str, target, diagnostic: str
) -> None:
    data = declaration_data()
    data["targets"][field] = target
    declaration = AppDeclaration.model_validate(data)

    with pytest.raises(ValueError, match=diagnostic):
        run_owned_check(name, tmp_path, declaration)


def test_report_validation_recomputes_hashes_and_subjects(tmp_path: Path) -> None:
    candidate = identity()
    sbom = tmp_path / "evidence/app.spdx.json"
    provenance = tmp_path / "evidence/provenance.json"
    sbom.parent.mkdir()
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    bind_sbom(sbom, candidate)
    write_provenance(provenance, candidate)
    report = CertificationReport.passed(candidate, sbom, provenance, root=tmp_path)
    path = tmp_path / "report.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    assert validate_report(
        path, root=tmp_path
    ).evidence.candidate_identity_digest == identity_digest(candidate)
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="SBOM.*subject|sha256"):
        validate_report(path, root=tmp_path)


def test_signed_attestation_replaces_and_rebinds_unsigned_predicate(tmp_path: Path) -> None:
    candidate = identity()
    sbom = tmp_path / "evidence/app.spdx.json"
    provenance = tmp_path / "evidence/provenance.json"
    sbom.parent.mkdir()
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")
    bind_sbom(sbom, candidate)
    write_provenance(provenance, candidate)
    report_path = tmp_path / "report.json"
    write_report(
        CertificationReport.passed(candidate, sbom, provenance, root=tmp_path), report_path
    )
    statement = provenance.read_bytes()
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps({"dsseEnvelope": {"payload": base64.b64encode(statement).decode()}}),
        encoding="utf-8",
    )

    final = finalize_attestation(bundle, report_path, tmp_path)

    assert final.evidence is not None
    assert provenance.read_bytes() == bundle.read_bytes()
    assert validate_report(report_path, root=tmp_path).status == "passed"


def test_handoff_recomputes_docker_archive_config_digest(tmp_path: Path) -> None:
    config = b'{"architecture":"amd64"}'
    digest = "sha256:" + hashlib.sha256(config).hexdigest()
    candidate = identity().model_copy(update={"image_digest": digest})
    archive_path = tmp_path / "image.tar"
    manifest = json.dumps([{"Config": f"{digest[7:]}.json"}]).encode()
    with tarfile.open(archive_path, "w") as archive:
        for name, content in (("manifest.json", manifest), (f"{digest[7:]}.json", config)):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    validate_image_archive(archive_path, candidate)
    with pytest.raises(ValueError, match="config digest"):
        validate_image_archive(archive_path, identity())


def test_structurally_bogus_report_is_rejected() -> None:
    raw = {
        "schema_version": 1,
        "status": "passed",
        "candidate": {
            "app_name": "app",
            "app_commit": "bogus",
            "zeroth_version": "bogus",
            "image_reference": "app",
            "image_digest": "bogus",
        },
        "checks": [],
        "evidence": None,
    }
    with pytest.raises(ValidationError):
        CertificationReport.model_validate(raw)


def test_scaffold_emits_valid_executable_assets(tmp_path: Path) -> None:
    scaffold_checkout(
        tmp_path,
        app_name="sample",
        module="sample_app",
        zeroth_version="0.23.9.1",
        zeroth_ref=COMMIT,
    )

    declaration = AppDeclaration.model_validate_json(
        (tmp_path / "certification.json").read_text(encoding="utf-8")
    )
    assert declaration.app_name == "sample"
    assert (tmp_path / ".github/workflows/app-certification.yml").is_file()
    assert (tmp_path / "Dockerfile.certification").is_file()
    assert (tmp_path / "sample_app/certification_healthcheck.py").is_file()
    assert "checks" not in json.loads((tmp_path / "certification.json").read_text())


def test_container_healthcheck_rejects_http_200_with_unhealthy_body(monkeypatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return b'{"status":"unhealthy"}'

    monkeypatch.setattr(certification_healthcheck, "urlopen", lambda *args, **kwargs: Response())
    assert certification_healthcheck.main() == 1


def test_optional_extras_rejects_the_certifier_environment(tmp_path: Path) -> None:
    declaration = AppDeclaration.model_validate(declaration_data())

    with pytest.raises(ValueError, match=r"must run with.*\.venv"):
        run_owned_check("optional-extras", tmp_path, declaration)


def test_migration_check_propagates_a_real_migration_failure(tmp_path: Path, monkeypatch) -> None:
    declaration = AppDeclaration.model_validate(declaration_data())

    def fail_migration(url: str) -> None:
        raise RuntimeError(f"invalid migration at {url}")

    monkeypatch.setattr(owned_checks, "run_migrations", fail_migration)
    with pytest.raises(RuntimeError, match="invalid migration"):
        run_owned_check("migrations", tmp_path, declaration)


@pytest.mark.parametrize(
    ("name", "states", "diagnostic"),
    [
        ("container-startup", [{"Running": False}, {"Running": True}], "not running"),
        (
            "health",
            [
                {"Running": True, "Health": {"Status": "unhealthy"}},
                {"Running": True, "Health": {"Status": "healthy"}},
            ],
            "not healthy",
        ),
    ],
)
def test_container_state_checks_reject_semantic_failures(
    tmp_path: Path, monkeypatch, name: str, states: list[dict], diagnostic: str
) -> None:
    declaration = AppDeclaration.model_validate(declaration_data())
    monkeypatch.setattr(owned_checks, "_container_states", lambda root: states)

    with pytest.raises(ValueError, match=diagnostic):
        run_owned_check(name, tmp_path, declaration)


def test_frontend_check_retains_stale_contract_diagnostic(tmp_path: Path, monkeypatch) -> None:
    declaration = AppDeclaration.model_validate(declaration_data())

    def stale(*args, **kwargs) -> None:
        raise ValueError("owned command exited 1: DRIFT: app/lib/api-types.ts")

    monkeypatch.setattr(owned_checks, "_run", stale)
    with pytest.raises(ValueError, match="DRIFT: app/lib/api-types.ts"):
        run_owned_check("frontend-api", tmp_path, declaration)
