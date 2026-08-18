from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from release.app_certification import AppDeclaration, load_declaration
from release.app_certification import cli as certification_cli_module
from release.app_certification.cli import main as certification_main
from release.app_certification.cli import resolve_smoke_headers
from tests.app_certification.test_engine import COMMIT, DIGEST, declaration_data


def test_unknown_fields_and_invalid_target_are_rejected() -> None:
    data = declaration_data()
    data["future_option"] = True
    with pytest.raises(ValidationError, match="future_option"):
        AppDeclaration.model_validate(data)
    data = declaration_data()
    data["targets"]["graph_builders"] = []
    with pytest.raises(ValidationError, match="graph_builders"):
        AppDeclaration.model_validate(data)


def test_json_loader_rejects_duplicate_target_names(tmp_path: Path) -> None:
    data = declaration_data()
    targets = json.dumps(data["targets"])[1:-1]
    raw = json.dumps({key: value for key, value in data.items() if key != "targets"})
    raw = raw[:-1] + f',"targets":{{{targets},"contracts":"second:value"}}}}'
    path = tmp_path / "certification.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key 'contracts'"):
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
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setattr(
        certification_cli_module,
        "_validate_direct_run_isolation",
        lambda *_args: "app-cert-candidate",
    )
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
            "--source-digest",
            "sha256:" + "c" * 64,
            "--packaged-url",
            "http://127.0.0.1:18080",
            "--ephemeral-url",
            "http://127.0.0.1:18081",
            "--report",
            str(report),
            "--untrusted-user",
            "app-cert-candidate",
            "--evidence-root",
            str(evidence),
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
