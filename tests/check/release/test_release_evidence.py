from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from release.check.models import REQUIRED_GATES, REQUIRED_SCHEMAS, CheckReleaseEvidenceV1
from release.check.release_evidence import _one_wheel, validate_artifact


def _wheel(directory: Path, *, version: str = "0.23.8.1.3") -> Path:
    path = directory / f"zeroth_core-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"zeroth_core-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: zeroth-core\nVersion: {version}\n",
        )
        for name in (
            "zeroth/check/tape/models.py",
            "zeroth/check/faults/models.py",
            "zeroth/check/verdict/models.py",
            "zeroth/check/adapter/langgraph.py",
        ):
            archive.writestr(name, "")
    return path


def _payload(wheel: Path) -> dict[str, object]:
    return {
        "schema_version": "check_release_evidence.v1",
        "source_commit": "a" * 40,
        "package_version": "0.23.8.1.3",
        "schemas": REQUIRED_SCHEMAS,
        "adapter": {"name": "langgraph", "version": "1", "dependency_version": "1.2.9"},
        "wheel": {"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "golden_fixture_sha256": {"fixture.json": "b" * 64},
        "gates": tuple(
            {"command": command, "exit_status": 0, "completed_at": "2026-08-19T18:00:00Z"}
            for command in REQUIRED_GATES
        ),
    }


def test_strict_evidence_binds_all_schemas_gates_and_exact_wheel(tmp_path) -> None:
    wheel = _wheel(tmp_path)
    payload = _payload(wheel)
    evidence = CheckReleaseEvidenceV1.model_validate(payload)
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence.model_dump(mode="json")))
    validate_artifact(path, tmp_path)


def test_rejects_missing_schema_failed_gate_and_unknown_field(tmp_path) -> None:
    wheel = _wheel(tmp_path)
    payload = _payload(wheel)
    payload["schemas"] = {"tape": "tape.v1"}
    with pytest.raises(ValidationError):
        CheckReleaseEvidenceV1.model_validate(payload)
    payload = _payload(wheel)
    gates = list(payload["gates"])
    gates[0] = gates[0] | {"exit_status": 1}
    payload["gates"] = tuple(gates)
    with pytest.raises(ValidationError):
        CheckReleaseEvidenceV1.model_validate(payload)
    payload = _payload(wheel) | {"prose_approval": True}
    with pytest.raises(ValidationError):
        CheckReleaseEvidenceV1.model_validate(payload)


def test_requires_exactly_one_wheel(tmp_path) -> None:
    with pytest.raises(ValueError):
        _one_wheel(tmp_path)
    _wheel(tmp_path)
    _wheel(tmp_path, version="9.9.9")
    with pytest.raises(ValueError):
        _one_wheel(tmp_path)
