from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from release.live_evaluation.template_provisioning_cli import (
    EXACT_CONFIG,
    FROZEN_D012_IDENTITY,
    ProvisioningBlockedError,
    build_parser,
    main,
    provision_live_template,
    validate_loopback_base_url,
    validate_service_api_key_file,
)


class _Response:
    def __init__(self, status_code: int, value: object) -> None:
        self.status_code = status_code
        self._value = value
        self.text = "<redacted>"

    def json(self) -> object:
        return self._value


class _Service:
    def __init__(self, *, collision: tuple[str, object] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.collision = collision
        self.health_reads = 0

    def __call__(self, method: str, path: str, payload: dict[str, Any] | None) -> _Response:
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/health":
            self.health_reads += 1
            return _Response(200, {"status": "ok", **FROZEN_D012_IDENTITY})
        if method == "GET" and path == "/v1/templates":
            rows: list[object] = []
            if self.collision and self.collision[0] == "template":
                rows.append(self.collision[1])
            return _Response(200, {"templates": rows})
        if method == "GET" and path in {
            "/api/studio/v1/contracts",
            "/api/studio/v1/workflows",
            "/v1/deployments",
        }:
            rows = []
            collision_kind = {
                "/api/studio/v1/contracts": "contract",
                "/api/studio/v1/workflows": "workflow",
                "/v1/deployments": "deployment",
            }[path]
            if self.collision and self.collision[0] == collision_kind:
                rows.append(self.collision[1])
            return _Response(200, rows)
        if method == "POST" and path == "/v1/templates":
            return _Response(201, {"name": EXACT_CONFIG.template_name, "version": 1})
        if method == "POST" and path == "/api/studio/v1/contracts":
            assert payload is not None
            return _Response(201, {"name": payload["name"], "version": 1})
        if method == "POST" and path == "/api/studio/v1/workflows":
            return _Response(201, {"id": "workflow-id"})
        if method == "PUT" and path == "/api/studio/v1/workflows/workflow-id":
            return _Response(200, {"id": "workflow-id"})
        if method == "POST" and path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if method == "POST" and path.endswith("/publish"):
            return _Response(200, {"status": "published", "version": 1})
        if method == "POST" and path == "/v1/deployments":
            return _Response(
                201,
                {
                    "deployment_ref": EXACT_CONFIG.deployment_ref,
                    "version": 1,
                    "graph_version_ref": "workflow-id@1",
                },
            )
        raise AssertionError(f"unexpected request {method} {path}")


def test_provisions_only_after_empty_scan_and_preserves_frozen_d012() -> None:
    service = _Service()

    result = provision_live_template(request=service)

    assert result["status"] == "provisioned"
    assert result["pre_health"] == {"status": "ok", **FROZEN_D012_IDENTITY}
    assert result["post_health"] == result["pre_health"]
    assert result["fixture"]["provider_calls_performed"] == 0
    paths = [path for _, path, _ in service.calls]
    assert paths[:5] == [
        "/health",
        "/v1/templates",
        "/api/studio/v1/contracts",
        "/api/studio/v1/workflows",
        "/v1/deployments",
    ]
    assert paths[-1] == "/health"
    assert not any("run" in path or "restart" in path or "provider" in path for path in paths)


@pytest.mark.parametrize(
    ("kind", "row"),
    [
        ("template", {"name": EXACT_CONFIG.template_name, "version": 1}),
        ("contract", {"name": f"contract://{EXACT_CONFIG.fixture_id}.probe"}),
        ("workflow", {"name": f"Live template render {EXACT_CONFIG.fixture_id}"}),
        ("deployment", {"deployment_ref": EXACT_CONFIG.deployment_ref}),
    ],
)
def test_preexisting_partial_fixture_blocks_before_any_mutation(kind: str, row: object) -> None:
    service = _Service(collision=(kind, row))

    with pytest.raises(ProvisioningBlockedError, match="fixture-preexists"):
        provision_live_template(request=service)

    assert all(method == "GET" for method, _, _ in service.calls)


def test_wrong_pre_health_blocks_before_collision_scan() -> None:
    service = _Service()

    def wrong_health(method: str, path: str, payload: dict[str, Any] | None) -> _Response:
        if path == "/health":
            service.calls.append((method, path, payload))
            return _Response(200, {"status": "ok", **FROZEN_D012_IDENTITY, "deployment_version": 2})
        return service(method, path, payload)

    with pytest.raises(ProvisioningBlockedError, match="frozen-d012-mismatch"):
        provision_live_template(request=wrong_health)

    assert service.calls == [("GET", "/health", None)]


def test_private_service_key_must_be_exact_regular_file_outside_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    private = tmp_path / "private-key"
    private.write_text("service-only-secret\n", encoding="utf-8")
    private.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert validate_service_api_key_file(private, repository_root=repository) == private.resolve()

    private.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)
    with pytest.raises(ProvisioningBlockedError, match="service-key-file-not-private"):
        validate_service_api_key_file(private, repository_root=repository)

    in_repo = repository / "key"
    in_repo.write_text("secret", encoding="utf-8")
    in_repo.chmod(stat.S_IRUSR | stat.S_IWUSR)
    with pytest.raises(ProvisioningBlockedError, match="service-key-file-inside-repository"):
        validate_service_api_key_file(in_repo, repository_root=repository)


def test_cli_surface_has_no_provider_credential_or_live_execution_inputs() -> None:
    parser = build_parser()
    option_strings = {option for action in parser._actions for option in action.option_strings}
    assert option_strings == {
        "-h",
        "--help",
        "--service-base-url",
        "--service-api-key-file",
        "--output-json",
    }
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--service-base-url",
                "http://127.0.0.1:8122",
                "--service-api-key-file",
                "/private/service-key",
                "--provider-api-key-file",
                "/private/provider-key",
            ]
        )


def test_only_numeric_http_loopback_with_explicit_port_is_accepted() -> None:
    assert validate_loopback_base_url("http://127.0.0.1:8122") == "http://127.0.0.1:8122"
    for unsafe in (
        "https://127.0.0.1:8122",
        "http://localhost:8122",
        "http://192.0.2.1:8122",
        "http://127.0.0.1",
        "http://user:password@127.0.0.1:8122",
        "http://127.0.0.1:8122/v1",
    ):
        with pytest.raises(ProvisioningBlockedError, match="service-base-url-not-loopback"):
            validate_loopback_base_url(unsafe)


def test_cli_failure_does_not_echo_sensitive_path(capsys: pytest.CaptureFixture[str]) -> None:
    sentinel = "/private/service-key-SHOULD-NOT-ESCAPE"
    exit_code = main(
        [
            "--service-base-url",
            "http://127.0.0.1:8122",
            "--service-api-key-file",
            sentinel,
        ]
    )

    output = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(output.out) == {
        "reason": "service-key-file-unavailable",
        "status": "blocked",
    }
    assert sentinel not in output.out
    assert sentinel not in output.err


def test_result_is_json_serializable_and_contains_no_secret_field_names() -> None:
    result = provision_live_template(request=_Service())
    rendered = json.dumps(result, sort_keys=True)
    assert "api_key" not in rendered.lower()
    assert "credential" not in rendered.lower()
    assert "secret" not in rendered.lower()
