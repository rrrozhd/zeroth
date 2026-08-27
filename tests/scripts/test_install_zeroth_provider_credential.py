from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/install_zeroth_provider_credential.py"


def _module():
    spec = importlib.util.spec_from_file_location("install_zeroth_provider_credential", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installs_create_only_external_env_with_private_permissions(tmp_path: Path) -> None:
    installer = _module()
    repository = tmp_path / "repository"
    repository.mkdir()
    destination = tmp_path / "state/runtime-secrets/provider.env"

    result = installer.install_credential(
        destination=destination,
        repository_root=repository,
        credential="opaque-provider-credential-value",
    )

    assert result == {
        "destination": str(destination.resolve()),
        "mode": "0600",
        "provider": "openai",
        "secret_persisted": True,
    }
    assert destination.read_text(encoding="utf-8") == (
        "OPENAI_API_KEY=opaque-provider-credential-value\n"
    )
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700

    with pytest.raises(FileExistsError):
        installer.install_credential(
            destination=destination,
            repository_root=repository,
            credential="different-provider-credential",
        )


@pytest.mark.parametrize(
    "credential",
    (
        "",
        "short",
        "contains whitespace value",
        "contains-newline\nvalue",
        "contains=separator-value",
    ),
)
def test_rejects_invalid_credential_without_creating_file(
    tmp_path: Path, credential: str
) -> None:
    installer = _module()
    repository = tmp_path / "repository"
    repository.mkdir()
    destination = tmp_path / "state/runtime-secrets/provider.env"

    with pytest.raises(ValueError, match="credential"):
        installer.install_credential(
            destination=destination,
            repository_root=repository,
            credential=credential,
        )

    assert not destination.exists()


def test_rejects_repository_destination_and_never_serializes_credential(
    tmp_path: Path,
) -> None:
    installer = _module()
    repository = tmp_path / "repository"
    repository.mkdir()
    destination = repository / ".dev-secrets/provider.env"

    with pytest.raises(ValueError, match="outside the repository"):
        installer.install_credential(
            destination=destination,
            repository_root=repository,
            credential="opaque-provider-credential-value",
        )

    assert not destination.exists()


def test_compose_uses_one_external_env_reference_for_both_tenants() -> None:
    document = yaml.safe_load((ROOT / "compose.dev.yml").read_text(encoding="utf-8"))

    for service_name in ("backend", "backend-twin"):
        service = document["services"][service_name]
        assert service["env_file"] == [
            {
                "path": "${ZEROTH_DEV_ENV_FILE:-.dev-secrets/zeroth.env}",
                "required": True,
            }
        ]
        assert "${ZEROTH_DEV_ENV_FILE:-.dev-secrets/zeroth.env}:/run/secrets/zeroth.env:ro" in service[
            "volumes"
        ]


def test_cli_result_contract_contains_no_secret_value(tmp_path: Path, monkeypatch, capsys) -> None:
    installer = _module()
    repository = tmp_path / "repository"
    repository.mkdir()
    destination = tmp_path / "state/runtime-secrets/provider.env"
    monkeypatch.setattr(installer.getpass, "getpass", lambda _: "opaque-provider-value-123")

    assert (
        installer.main(
            [
                "--destination",
                str(destination),
                "--repository-root",
                str(repository),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["secret_persisted"] is True
    assert "opaque-provider-value-123" not in output
