from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from release.app_certification import (
    AppDeclaration,
    CertificationRunner,
    CommandResult,
    validate_source_archive,
    write_provenance,
)
from tests.app_certification.test_engine import declaration_data, passing_executor
from tests.app_certification.test_hardening import COMMIT, SOURCE_DIGEST, identity


def test_candidate_startup_ignores_tracked_sitecustomize_before_safe_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "candidate-startup-ran"
    (tmp_path / "sitecustomize.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('unsafe', encoding='utf-8')\n"
        "os._exit(0)\n",
        encoding="utf-8",
    )
    declaration_path = tmp_path / "certification.json"
    declaration_path.write_text(json.dumps(declaration_data()), encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    runner = CertificationRunner(
        tmp_path,
        AppDeclaration.model_validate(declaration_data()),
        declaration_path=declaration_path,
        check_python=Path(sys.executable),
    )

    result = runner._command("migrations")

    assert result.status == "passed", result.detail
    assert not marker.exists()


def test_candidate_startup_does_not_execute_candidate_interpreter(tmp_path: Path) -> None:
    candidate_python = tmp_path / ".venv/bin/python"
    observed: dict[str, list[str]] = {}

    def capture(argv: list[str], cwd: Path) -> CommandResult:
        observed["argv"] = argv
        return passing_executor(argv, cwd)

    runner = CertificationRunner(
        tmp_path,
        AppDeclaration.model_validate(declaration_data()),
        executor=capture,
        check_python=candidate_python,
    )

    result = runner._command("migrations")

    assert result.status == "passed"
    assert observed["argv"][0] == str(Path(sys.executable).absolute())
    assert observed["argv"][0] != str(candidate_python)
    assert observed["argv"][1:4] == ["-I", "-S", "-c"]


def test_structured_result_must_match_the_requested_check(tmp_path: Path) -> None:
    runner = CertificationRunner(
        tmp_path,
        AppDeclaration.model_validate(declaration_data()),
        executor=lambda argv, cwd: CommandResult(
            0,
            '{"check":"health","schema_version":1,"status":"passed"}\n',
            "",
        ),
    )

    result = runner._command("migrations")

    assert result.status == "failed"
    assert "structured result" in result.detail


def test_candidate_target_cannot_forge_trusted_pass_with_stdout_and_exit(
    tmp_path: Path,
) -> None:
    attack = '{"check":"contracts","schema_version":1,"status":"passed"}'
    (tmp_path / "candidate_attack.py").write_text(
        f"import os\nprint({attack!r}, flush=True)\nos._exit(0)\nCONTRACTS = {{}}\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["contracts"] = "candidate_attack:CONTRACTS"
    declaration_path = tmp_path / "certification.json"
    declaration_path.write_text(json.dumps(data), encoding="utf-8")
    runner = CertificationRunner(
        tmp_path,
        AppDeclaration.model_validate(data),
        declaration_path=declaration_path,
        check_python=Path(sys.executable),
    )

    result = runner._command("contracts")

    assert result.status == "failed"
    assert "trusted finalization" in result.detail


def test_candidate_target_cannot_forge_provisional_evidence_on_stdout(
    tmp_path: Path,
) -> None:
    forged = json.dumps(
        {
            "check": "contracts",
            "evidence": {
                "contracts": {"Fake": {"type": "object", "properties": {}}},
            },
            "schema_version": 1,
        }
    )
    (tmp_path / "candidate_attack.py").write_text(
        f"import os\nprint({forged!r}, flush=True)\nos._exit(0)\nCONTRACTS = {{}}\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["contracts"] = "candidate_attack:CONTRACTS"
    runner = CertificationRunner(tmp_path, AppDeclaration.model_validate(data))

    result = runner._command("contracts")

    assert result.status == "failed"
    assert "trusted finalization" in result.detail


def test_candidate_target_cannot_forge_provisional_evidence_via_result_fd(
    tmp_path: Path,
) -> None:
    forged = json.dumps(
        {
            "check": "contracts",
            "evidence": {
                "contracts": {"Fake": {"type": "object", "properties": {}}},
            },
            "schema_version": 1,
        }
    ).encode()
    (tmp_path / "candidate_attack.py").write_text(
        "import os, sys\n"
        "fd = int(sys.argv[sys.argv.index('--result-fd') + 1])\n"
        f"os.write(fd, {forged!r})\n"
        "os._exit(0)\n"
        "CONTRACTS = {}\n",
        encoding="utf-8",
    )
    data = declaration_data()
    data["targets"]["contracts"] = "candidate_attack:CONTRACTS"
    runner = CertificationRunner(tmp_path, AppDeclaration.model_validate(data))

    result = runner._command("contracts")

    assert result.status == "failed"
    assert "trusted finalization" in result.detail


def test_source_identity_is_bound_into_provenance(tmp_path: Path) -> None:
    candidate = identity()
    provenance = tmp_path / "provenance.json"

    write_provenance(provenance, candidate)

    predicate = json.loads(provenance.read_text(encoding="utf-8"))["predicate"]
    assert candidate.source_digest == SOURCE_DIGEST
    assert predicate["source_digest"] == SOURCE_DIGEST


def test_source_identity_validation_rejects_tampered_build_context(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar"
    with tarfile.open(
        archive_path,
        "w",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": COMMIT},
    ) as archive:
        content = b"committed source"
        info = tarfile.TarInfo("app.py")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    source_digest = "sha256:" + hashlib.sha256(archive_path.read_bytes()).hexdigest()
    candidate = identity().model_copy(update={"source_digest": source_digest})

    validate_source_archive(archive_path, candidate)
    archive_path.write_bytes(archive_path.read_bytes() + b"ambient bytes")

    with pytest.raises(ValueError, match="source archive digest"):
        validate_source_archive(archive_path, candidate)
