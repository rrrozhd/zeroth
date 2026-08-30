from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Mount, Route, WebSocketRoute

from zeroth.integrations.execution.models import (
    BuildConfig,
    ExecutionMode,
    InputMode,
    OutputMode,
    ProjectArchiveArtifactSource,
    ProjectUnitManifest,
    RunConfig,
)
from zeroth.integrations.execution.runner import (
    ExecutableUnitBinding,
    ExecutableUnitExecutionError,
    ExecutableUnitRunner,
)
from zeroth.service.app import create_app

INVENTORY = Path(__file__).resolve().parents[2] / "release/security/public-route-inventory.json"
LIVE_INVENTORY = Path(__file__).resolve().parents[2] / "release/security/live-route-inventory.json"
HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
REPOSITORY_INGRESS_PREFIX = "/v1/repos"


def _current_public_routes() -> list[dict[str, str]]:
    with patch.dict(os.environ, {"ZEROTH_CONSOLE_DIR": "/__zeroth_no_console__"}):
        schema = create_app(SimpleNamespace(regulus_client=None)).openapi()
    return sorted(
        (
            {"method": method.upper(), "path": path, "operation_id": operation["operationId"]}
            for path, path_item in schema["paths"].items()
            for method, operation in path_item.items()
            if method.upper() in HTTP_METHODS
        ),
        key=lambda route: (route["method"], route["path"], route["operation_id"]),
    )


def test_complete_public_openapi_inventory_matches_reviewed_snapshot() -> None:
    assert _current_public_routes() == json.loads(INVENTORY.read_text(encoding="utf-8"))


def test_disabled_public_inventory_has_no_repository_ingress_capability() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    assert not any(route["path"].startswith(REPOSITORY_INGRESS_PREFIX) for route in inventory)


class _GitHubIntegration:
    async def drop_installation_caches(self, _installation_id: int) -> None:
        return None


def _bootstrap(*, gateway: bool, regulus: bool = False, github: bool = False) -> SimpleNamespace:
    values = {"regulus_client": object() if regulus else None}
    if github:
        values.update(
            github_integration_service=_GitHubIntegration(),
            github_webhook_secret_resolver=object(),
            repository_unit_service=object(),
        )
    if gateway:
        values.update(
            langgraph_gateway_proxy=object(),
            langgraph_gateway_websocket_handler=object(),
            langgraph_gateway_compatibility=None,
            authenticator=object(),
        )
    return SimpleNamespace(**values)


def _live_routes(app) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []

    def visit(routes, prefix: str = "") -> None:
        for route in routes:
            path = f"{prefix}{getattr(route, 'path', '')}" or "/"
            if isinstance(route, Mount):
                inventory.append({"kind": "mount", "path": path, "methods": []})
                visit(getattr(route, "routes", ()), path.rstrip("/"))
            elif isinstance(route, WebSocketRoute):
                inventory.append({"kind": "websocket", "path": path, "methods": []})
            else:
                inventory.append(
                    {
                        "kind": "http",
                        "path": path,
                        "methods": sorted(getattr(route, "methods", ()) or ()),
                    }
                )

    visit(app.routes)
    return sorted(
        inventory,
        key=lambda item: (str(item["kind"]), str(item["path"]), item["methods"]),
    )


def _configured_live_inventory() -> dict[str, list[dict[str, object]]]:
    with tempfile.TemporaryDirectory() as directory:
        console = Path(directory)
        (console / "index.html").write_text("console", encoding="utf-8")
        configurations: dict[str, list[dict[str, object]]] = {}
        for name, gateway, regulus, github, console_dir in (
            ("default", False, False, False, "/__zeroth_no_console__"),
            ("gateway", True, False, False, "/__zeroth_no_console__"),
            ("console", False, False, False, str(console)),
            ("gateway-console", True, False, False, str(console)),
            ("regulus", False, True, False, "/__zeroth_no_console__"),
            ("github", False, False, True, "/__zeroth_no_console__"),
            ("all-features", True, True, True, str(console)),
        ):
            with patch.dict(os.environ, {"ZEROTH_CONSOLE_DIR": console_dir}):
                configurations[name] = _live_routes(
                    create_app(_bootstrap(gateway=gateway, regulus=regulus, github=github))
                )
        return configurations


def test_complete_live_route_inventory_matches_all_reviewed_configurations() -> None:
    assert _configured_live_inventory() == json.loads(LIVE_INVENTORY.read_text())


def test_enabled_live_inventory_contains_repository_and_webhook_ingress() -> None:
    inventory = json.loads(LIVE_INVENTORY.read_text())
    enabled = inventory["github"]
    paths = {item["path"] for item in enabled}
    assert "/v1/repos/installations/{installation_id}/claim" in paths
    assert "/v1/repos/{repository_id}/checkouts" in paths
    assert "/v1/repos/checkouts/{checkout_id}/runs" in paths
    assert "/integrations/github/webhook" in paths
    disabled_paths = {item["path"] for item in inventory["default"]}
    assert not any(path.startswith(REPOSITORY_INGRESS_PREFIX) for path in disabled_paths)
    assert "/integrations/github/webhook" not in disabled_paths


@pytest.mark.parametrize("hidden_kind", ["http", "websocket", "mount", "conditional"])
def test_hidden_route_kinds_invalidate_complete_inventory(hidden_kind: str) -> None:
    app = create_app(_bootstrap(gateway=hidden_kind == "conditional"))
    baseline = json.loads(LIVE_INVENTORY.read_text())[
        "gateway" if hidden_kind == "conditional" else "default"
    ]
    if hidden_kind in {"http", "conditional"}:
        app.router.routes.append(Route("/repositories", lambda _request: None))
    elif hidden_kind == "websocket":
        app.router.routes.append(WebSocketRoute("/repositories", lambda _socket: None))
    else:
        nested = Starlette(routes=[Route("/repositories", lambda _request: None)])
        app.router.routes.append(Mount("/hidden", app=nested))
    assert _live_routes(app) != baseline


class _Input(BaseModel):
    value: str


class _Output(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_project_manifest_refuses_before_build_without_trusted_materializer(
    tmp_path: Path,
) -> None:
    build_marker = tmp_path / "build-reached"
    manifest = ProjectUnitManifest(
        unit_id="unmaterialized-project",
        onboarding_mode=ExecutionMode.PROJECT,
        runtime="project",
        artifact_source=ProjectArchiveArtifactSource(ref="github://owner/repository"),
        build_config=BuildConfig(
            command=[
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(build_marker)!r}).touch()",
            ]
        ),
        run_config=RunConfig(command=[sys.executable, "-c", 'print(\'{"value":"ran"}\')']),
        project_archive_ref="github://owner/repository",
        entrypoint_type="project",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        cache_identity_fields={"project": "unmaterialized"},
    )
    binding = ExecutableUnitBinding(
        manifest_ref="eu://unmaterialized-project",
        manifest=manifest,
        input_model=_Input,
        output_model=_Output,
    )

    with pytest.raises(ExecutableUnitExecutionError, match="trusted project materializer"):
        await ExecutableUnitRunner().run_binding(binding, {"value": "input"})

    assert not build_marker.exists()
