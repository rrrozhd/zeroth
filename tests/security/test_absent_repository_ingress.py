from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import BaseModel

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
HTTP_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
REPOSITORY_INGRESS_TERMS = (
    "github",
    "repository",
    "installation",
    "checkout",
    "/sources",
    "/imports",
)


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


def test_reviewed_public_inventory_has_no_repository_ingress_capability() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    searchable = "\n".join(
        f"{route['method']} {route['path']} {route['operation_id']}" for route in inventory
    ).lower()
    assert all(term not in searchable for term in REPOSITORY_INGRESS_TERMS)


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
