from __future__ import annotations

from pathlib import Path

from release.live_evaluation.resilient_http_live import (
    ResilientHttpDockerController,
    ResilientHttpFixture,
    ServingIdentity,
    execute_browser_journey,
    provision_resilient_http_fixture,
    scenario_peer_ready,
)


class _Response:
    def __init__(self, status_code: int, value: object) -> None:
        self.status_code = status_code
        self.value = value
        self.text = "safe"

    def json(self) -> object:
        return self.value


def test_fixture_publishes_exact_private_get_branches() -> None:
    saved: list[dict[str, object]] = []

    def request(method: str, path: str, payload: dict[str, object] | None) -> _Response:
        if method == "POST" and path == "/api/studio/v1/contracts":
            assert payload is not None
            return _Response(201, {"name": payload["name"], "version": 1})
        if method == "POST" and path == "/api/studio/v1/workflows":
            return _Response(201, {"id": "http-workflow"})
        if method == "PUT" and path == "/api/studio/v1/workflows/http-workflow":
            assert payload is not None
            saved.append(payload)
            return _Response(200, {"id": "http-workflow"})
        if method == "POST" and path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if method == "POST" and path.endswith("/publish"):
            return _Response(200, {"status": "published", "version": 1})
        if method == "POST" and path == "/v1/deployments":
            return _Response(
                201,
                {
                    "deployment_ref": "provider-free-resilient-http-http-live-a",
                    "version": 1,
                    "graph_version_ref": "http-workflow@1",
                },
            )
        raise AssertionError((method, path))

    fixture = provision_resilient_http_fixture(request=request, fixture_id="http-live-a")

    assert fixture.workflow_id == "http-workflow"
    assert fixture.graph_version_ref == "http-workflow@1"
    assert fixture.provider_calls_performed == 0
    nodes = saved[0]["nodes"]
    assert isinstance(nodes, list)
    by_id = {node["id"]: node for node in nodes}
    assert by_id["http-retry"]["data"]["config"] == {
        "method": "GET",
        "url": "http://127.0.0.1:8787/scenario/retry-then-success",
        "timeout_seconds": 1.0,
        "max_retries": 2,
        "retryable_status_codes": [503],
        "max_response_bytes": 4096,
    }
    assert by_id["http-timeout"]["data"]["config"]["timeout_seconds"] == 0.05
    assert by_id["http-circuit"]["data"]["config"]["max_retries"] == 0
    assert by_id["route-retry"]["data"]["config"] == {"expression": "payload.scenario == 'retry'"}
    assert by_id["route-timeout"]["data"]["config"] == {
        "expression": "payload.scenario == 'timeout'"
    }
    assert {edge.get("source_handle") for edge in saved[0]["edges"]} >= {"true", "false"}
    assert saved[0]["execution_settings"]["max_total_steps"] == 5


def test_controller_uses_scoped_override_and_restores_exact_d012(tmp_path: Path) -> None:
    compose = tmp_path / "compose.dev.yml"
    override = tmp_path / "compose.resilient-http-live.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    override.write_text("services: {}\n", encoding="utf-8")
    calls: list[tuple[tuple[str, ...], str | None]] = []
    observed = iter(
        (
            ServingIdentity("d012", 1, "d012@1"),
            ServingIdentity("http-live", 1, "http@1"),
            ServingIdentity("d012", 1, "d012@1"),
        )
    )

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(argv, *, cwd, env, capture_output, text, check, timeout):
        assert cwd == tmp_path
        assert (capture_output, text, check, timeout) == (True, True, False, 120)
        calls.append((tuple(argv), env.get("ZEROTH_DEV_DEPLOYMENT_REF")))
        return _Completed()

    controller = ResilientHttpDockerController(
        workspace=tmp_path,
        compose_file=compose,
        override_file=override,
        observe=lambda: next(observed),
        command_runner=runner,
        sleep=lambda _: None,
    )
    before = controller.capture()
    controller.serve(
        deployment_ref="http-live",
        deployment_version=1,
        graph_version_ref="http@1",
    )
    proof = controller.restore(before)

    assert proof["exact"] is True
    assert proof["before"] == proof["after"]
    assert any(
        any(argument.endswith("compose.resilient-http-live.yml") for argument in call[0])
        for call in calls
    )
    assert any("--wait-timeout" in call[0] and "60" in call[0] for call in calls)
    assert any(
        "exec" in call[0]
        and "backend" in call[0]
        and "release.live_evaluation.resilient_http_scenario_server" in call[0]
        for call in calls
    )
    assert calls[-1][1] == "d012"


def test_browser_runner_restores_exact_identity_when_playwright_fails(tmp_path: Path) -> None:
    actions: list[str] = []
    commands: list[tuple[str, ...]] = []

    class _Controller:
        def serve(self, **_: object) -> ServingIdentity:
            actions.append("serve")
            return ServingIdentity("http-live", 1, "http@1")

        def restore(self, before: ServingIdentity) -> dict[str, object]:
            actions.append("restore")
            return {
                "before": {
                    "deployment_ref": before.deployment_ref,
                    "deployment_version": before.deployment_version,
                    "graph_version_ref": before.graph_version_ref,
                },
                "after": {
                    "deployment_ref": before.deployment_ref,
                    "deployment_version": before.deployment_version,
                    "graph_version_ref": before.graph_version_ref,
                },
                "exact": True,
            }

    class _Completed:
        returncode = 1
        stdout = "one browser failure\n"
        stderr = ""

    fixture = ResilientHttpFixture(
        schema_version=1,
        fixture_id="http-live-a",
        workflow_id="http-workflow",
        graph_version_ref="http@1",
        deployment_ref="http-live",
        deployment_version=1,
    )
    source = tmp_path / "attempt"
    source.mkdir()

    def run_playwright(argv, **kwargs):
        commands.append(tuple(argv))
        assert kwargs["timeout"] == 360
        return _Completed()

    result = execute_browser_journey(
        controller=_Controller(),  # type: ignore[arg-type]
        before=ServingIdentity("d012", 1, "d012@1"),
        fixture=fixture,
        source_root=source,
        browser_environment={"SAFE": "1"},
        command_runner=run_playwright,
        scenario_probe=lambda: True,
    )

    assert result.returncode == 1
    assert actions == ["serve", "restore"]
    assert (source / "runtime/d012-restore.json").is_file()
    assert (source / "commands/playwright.exit.txt").read_text() == "1\n"
    assert len(commands) == 1
    assert commands[0].count("--project=desktop-1440") == 1
    assert commands[0].count("--project=webkit-1440") == 1


def test_readiness_uses_backend_namespace_only(tmp_path: Path) -> None:
    compose = tmp_path / "compose.dev.yml"
    override = tmp_path / "compose.resilient-http-live.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    override.write_text("services: {}\n", encoding="utf-8")

    class _Completed:
        returncode = 0

    def runner(argv, **kwargs):
        assert "exec" in argv and "backend" in argv
        assert kwargs["cwd"] == tmp_path
        return _Completed()

    assert scenario_peer_ready(
        workspace=tmp_path,
        compose_file=compose,
        override_file=override,
        command_runner=runner,
    )
