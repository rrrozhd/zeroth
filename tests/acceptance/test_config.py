from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from release.acceptance.config import AcceptanceConfig, ResolvedAcceptanceConfig


def _identity(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit": "a" * 40,
                "package": {"version": "1.2.3", "artifacts": {}},
                "image": {"registry.example/zeroth:test": "sha256:" + "d" * 64},
            }
        ),
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "base_url": "https://candidate.example.test",
        "tenant_id": "acceptance-tenant",
        "deployment_ref": "acceptance-deployment",
        "candidate_identity": str(_identity(tmp_path / "identity.json")),
        "credentials": {
            "operator": "ZEROTH_ACCEPTANCE_OPERATOR_KEY",
            "reviewer": "ZEROTH_ACCEPTANCE_REVIEWER_KEY",
            "admin": "ZEROTH_ACCEPTANCE_ADMIN_KEY",
        },
        "lifecycle": {
            "restart_url": "/__acceptance/restart",
            "shutdown_url": "/__acceptance/shutdown",
        },
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("base_url", "file:///tmp/socket", "HTTP"),
        ("base_url", "https://user:pass@example.test", "userinfo"),
        ("base_url", "https://candidate.example.test/root", "origin root"),
        ("tenant_id", "default", "dedicated"),
        ("tenant_id", "production", "acceptance-"),
    ],
)
def test_config_rejects_unsafe_target_values(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    config = _config(tmp_path, **{field: value})

    with pytest.raises(ValidationError, match=message):
        AcceptanceConfig.model_validate(config)


def test_config_requires_distinct_role_secret_references(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        credentials={
            "operator": "SHARED",
            "reviewer": "SHARED",
            "admin": "ADMIN",
        },
    )

    with pytest.raises(ValidationError, match="distinct"):
        AcceptanceConfig.model_validate(config)


def test_resolve_requires_every_secret_and_image_identity(tmp_path: Path) -> None:
    config = AcceptanceConfig.model_validate(_config(tmp_path))

    with pytest.raises(ValueError, match="ZEROTH_ACCEPTANCE_REVIEWER_KEY"):
        config.resolve(
            {
                "ZEROTH_ACCEPTANCE_OPERATOR_KEY": "operator-secret",
                "ZEROTH_ACCEPTANCE_ADMIN_KEY": "admin-secret",
            }
        )

    identity = json.loads(Path(config.candidate_identity).read_text(encoding="utf-8"))
    del identity["image"]
    Path(config.candidate_identity).write_text(json.dumps(identity), encoding="utf-8")
    with pytest.raises(ValueError, match="image"):
        config.resolve(
            {
                "ZEROTH_ACCEPTANCE_OPERATOR_KEY": "operator-secret",
                "ZEROTH_ACCEPTANCE_REVIEWER_KEY": "reviewer-secret",
                "ZEROTH_ACCEPTANCE_ADMIN_KEY": "admin-secret",
            }
        )


def test_resolved_config_creates_owned_namespace_and_guards_cleanup(tmp_path: Path) -> None:
    config = AcceptanceConfig.model_validate(_config(tmp_path)).resolve(
        {
            "ZEROTH_ACCEPTANCE_OPERATOR_KEY": "operator-secret",
            "ZEROTH_ACCEPTANCE_REVIEWER_KEY": "reviewer-secret",
            "ZEROTH_ACCEPTANCE_ADMIN_KEY": "admin-secret",
        },
        run_id="0123456789abcdef",
    )

    assert isinstance(config, ResolvedAcceptanceConfig)
    assert config.namespace == "acceptance-tenant-0123456789abcdef"
    assert config.owns(f"{config.namespace}-workflow")
    assert not config.owns("acceptance-tenant-another-run")
    config.require_owned(f"{config.namespace}-artifact")
    with pytest.raises(ValueError, match="outside acceptance namespace"):
        config.require_owned("production-artifact")


def test_config_does_not_serialize_resolved_secrets(tmp_path: Path) -> None:
    config = AcceptanceConfig.model_validate(_config(tmp_path)).resolve(
        {
            "ZEROTH_ACCEPTANCE_OPERATOR_KEY": "operator-secret",
            "ZEROTH_ACCEPTANCE_REVIEWER_KEY": "reviewer-secret",
            "ZEROTH_ACCEPTANCE_ADMIN_KEY": "admin-secret",
        }
    )

    serialized = config.model_dump_json()
    assert "operator-secret" not in serialized
    assert "reviewer-secret" not in serialized
    assert "admin-secret" not in serialized
