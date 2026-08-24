from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from zeroth.integrations.execution.models import (
    BuildConfig,
    CommandArtifactSource,
    ExecutionMode,
    InputMode,
    NativeUnitManifest,
    OutputMode,
    ProjectArchiveArtifactSource,
    ProjectUnitManifest,
    PythonModuleArtifactSource,
    RunConfig,
    WrappedCommandUnitManifest,
)
from zeroth.integrations.execution.runner import (
    ExecutableUnitBinding,
    ExecutableUnitExecutionError,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
)
from zeroth.integrations.execution.sandbox import SandboxManager


class DemoInput(BaseModel):
    name: str
    count: int


class DemoOutput(BaseModel):
    answer: str
    score: int


class ExitCodeOutput(BaseModel):
    exit_code: int


@pytest.mark.asyncio
async def test_wrapped_command_runner_supports_cli_args_and_json_stdout(tmp_path: Path) -> None:
    script = tmp_path / "cli_args.py"
    script.write_text(
        """
import json
import sys

argv = sys.argv[1:]
payload = dict(zip(argv[::2], argv[1::2], strict=True))
print(json.dumps({"answer": payload["--name"], "score": int(payload["--count"])}))
""".strip()
    )
    manifest = WrappedCommandUnitManifest(
        unit_id="cli-args-unit",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref=str(script)),
        entrypoint_type="command",
        input_mode=InputMode.CLI_ARGS,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(command=[sys.executable, str(script)]),
        cache_identity_fields={"script": script.name},
    )
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://cli-args",
            manifest=manifest,
            input_model=DemoInput,
            output_model=DemoOutput,
        )
    )

    result = await ExecutableUnitRunner(registry).run_manifest_ref(
        "eu://cli-args",
        DemoInput(name="alpha", count=3),
    )

    assert result.output_data == {"answer": "alpha", "score": 3}
    assert result.sandbox_result is not None
    assert result.sandbox_result.returncode == 0


@pytest.mark.asyncio
async def test_wrapped_command_runner_supports_env_vars_and_tagged_stdout(tmp_path: Path) -> None:
    script = tmp_path / "env_tagged.py"
    script.write_text(
        """
import os

print("log line")
print(
    "ZEROTH_OUTPUT_JSON="
    + '{"answer":"%s","score":%s}'
    % (os.environ["ZEROTH_INPUT_NAME"].strip('"'), os.environ["ZEROTH_INPUT_COUNT"])
)
""".strip()
    )
    manifest = WrappedCommandUnitManifest(
        unit_id="env-unit",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref=str(script)),
        entrypoint_type="command",
        input_mode=InputMode.ENV_VARS,
        output_mode=OutputMode.TAGGED_STDOUT_JSON,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(command=[sys.executable, str(script)]),
        cache_identity_fields={"script": script.name},
    )
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://env-unit",
            manifest=manifest,
            input_model=DemoInput,
            output_model=DemoOutput,
        )
    )

    result = await ExecutableUnitRunner(registry).run_manifest_ref(
        "eu://env-unit",
        {"name": "beta", "count": 4},
    )

    assert result.output_data == {"answer": "beta", "score": 4}
    assert result.extracted_output is not None
    assert result.extracted_output.stdout.startswith("log line")


@pytest.mark.asyncio
async def test_project_runner_builds_once_per_cache_key(
    tmp_path: Path,
) -> None:
    build_marker = tmp_path / "build-count.txt"
    script = tmp_path / "project.py"
    script.write_text(
        """
import json
import os
from pathlib import Path

input_file = Path(os.environ["ZEROTH_INPUT_FILE"])
output_file = Path(os.environ["ZEROTH_OUTPUT_FILE"])
payload = json.loads(input_file.read_text())
output_file.write_text(json.dumps({"answer": payload["name"], "score": payload["count"]}))
""".strip()
    )
    build_command = [
        sys.executable,
        "-c",
        (
            "import os, pathlib; "
            "p = pathlib.Path(os.environ['BUILD_MARKER']); "
            "count = int(p.read_text()) + 1 if p.exists() else 1; "
            "p.write_text(str(count))"
        ),
    ]
    manifest = ProjectUnitManifest(
        unit_id="project-unit",
        onboarding_mode=ExecutionMode.PROJECT,
        runtime="project",
        artifact_source=ProjectArchiveArtifactSource(ref=str(script)),
        build_config=BuildConfig(
            command=build_command,
            environment={"BUILD_MARKER": str(build_marker)},
        ),
        run_config=RunConfig(command=[sys.executable, str(script)]),
        project_archive_ref="archive://demo-project",
        entrypoint_type="project",
        input_mode=InputMode.INPUT_FILE_JSON,
        output_mode=OutputMode.OUTPUT_FILE_JSON,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        cache_identity_fields={"archive": "demo-project"},
    )
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://project-unit",
            manifest=manifest,
            input_model=DemoInput,
            output_model=DemoOutput,
        )
    )
    runner = ExecutableUnitRunner(registry, project_materializer=lambda _manifest, _cwd: None)

    first = await runner.run_manifest_ref("eu://project-unit", {"name": "gamma", "count": 5})
    second = await runner.run_manifest_ref("eu://project-unit", {"name": "delta", "count": 6})

    assert first.output_data == {"answer": "gamma", "score": 5}
    assert second.output_data == {"answer": "delta", "score": 6}
    assert build_marker.read_text() == "1"


@pytest.mark.asyncio
async def test_project_build_uses_binding_environment_allowlist() -> None:
    manifest = ProjectUnitManifest(
        unit_id="project-build-env",
        onboarding_mode=ExecutionMode.PROJECT,
        runtime="project",
        artifact_source=ProjectArchiveArtifactSource(ref="archive://project-build-env"),
        build_config=BuildConfig(
            command=[
                sys.executable,
                "-c",
                "import os,sys; sys.exit(9 if os.getenv('GITHUB_TOKEN') else 0)",
            ],
        ),
        run_config=RunConfig(
            command=[sys.executable, "-c", 'print(\'{"answer":"safe","score":1}\')']
        ),
        project_archive_ref="archive://project-build-env",
        entrypoint_type="project",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        cache_identity_fields={"archive": "project-build-env"},
    )
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://project-build-env",
            manifest=manifest,
            input_model=DemoInput,
            output_model=DemoOutput,
            allowed_env_keys=("PATH",),
        )
    )
    manager = SandboxManager(base_env={"PATH": os.environ["PATH"], "GITHUB_TOKEN": "fake"})

    result = await ExecutableUnitRunner(
        registry,
        sandbox_manager=manager,
        project_materializer=lambda _manifest, _cwd: None,
    ).run_manifest_ref("eu://project-build-env", {"name": "safe", "count": 1})

    assert result.output_data == {"answer": "safe", "score": 1}


def test_runner_rejects_symlink_escape_workdir(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ExecutableUnitExecutionError, match="contained"):
        ExecutableUnitRunner()._resolve_workdir(root, "escape/job")


@pytest.mark.asyncio
async def test_native_runner_uses_governai_python_tool_for_native_units() -> None:
    manifest = NativeUnitManifest(
        unit_id="native-unit",
        onboarding_mode=ExecutionMode.NATIVE,
        runtime="python",
        artifact_source=PythonModuleArtifactSource(ref="demo.native:handler"),
        callable_ref="demo.native:handler",
        entrypoint_type="python_callable",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        cache_identity_fields={"python": "3.12"},
    )
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://native-unit",
            manifest=manifest,
            input_model=DemoInput,
            output_model=DemoOutput,
            python_handler=lambda _ctx, data: {"answer": data.name, "score": data.count},
        )
    )

    result = await ExecutableUnitRunner(registry).run_manifest_ref(
        "eu://native-unit",
        DemoInput(name="native", count=9),
    )

    assert result.output_data == {"answer": "native", "score": 9}
    assert result.sandbox_result is None


@pytest.mark.asyncio
async def test_runner_allows_exit_code_only_for_non_zero_exit(tmp_path: Path) -> None:
    script = tmp_path / "exit.py"
    script.write_text("import sys; sys.exit(7)")
    manifest = WrappedCommandUnitManifest(
        unit_id="exit-code-unit",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref=str(script)),
        entrypoint_type="command",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.EXIT_CODE_ONLY,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(command=[sys.executable, str(script)]),
        cache_identity_fields={"script": script.name},
    )
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://exit-code-unit",
            manifest=manifest,
            input_model=DemoInput,
            output_model=ExitCodeOutput,
        )
    )

    result = await ExecutableUnitRunner(registry).run_manifest_ref(
        "eu://exit-code-unit",
        {"name": "noop", "count": 1},
    )

    assert result.output_data == {"exit_code": 7}


@pytest.mark.asyncio
async def test_runner_dispatches_sidecar_not_local(tmp_path: Path) -> None:
    """A SIDECAR-configured manager must reach the sidecar through the runner's
    prepared-environment dispatch — pre-fix it silently fell through to
    _run_locally, executing untrusted code on the host (audit P1)."""
    from unittest.mock import AsyncMock, Mock

    from zeroth.integrations.execution.sandbox import SandboxBackendMode, SandboxConfig
    from zeroth.integrations.sandbox.models import SidecarExecuteResponse

    # If the buggy local path is taken, the real script runs and prints THIS, so
    # the sidecar-vs-local outcome is an unambiguous data difference, not an error.
    script = tmp_path / "unit.py"
    script.write_text('print(\'{"answer": "local", "score": 0}\')')
    manifest = WrappedCommandUnitManifest(
        unit_id="sidecar-unit",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref=str(script)),
        entrypoint_type="command",
        input_mode=InputMode.CLI_ARGS,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(command=[sys.executable, str(script)]),
        cache_identity_fields={"script": script.name},
    )
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://sidecar-unit",
            manifest=manifest,
            input_model=DemoInput,
            output_model=DemoOutput,
            allowed_env_keys=("PATH",),
        )
    )
    sidecar_client = AsyncMock()
    sidecar_client.execute.return_value = SidecarExecuteResponse(
        execution_id="e",
        status="completed",
        returncode=0,
        stdout='{"answer": "sidecar", "score": 1}',
        stderr="",
        duration_seconds=0.1,
        timed_out=False,
    )
    manager = SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.SIDECAR),
        sidecar_client=sidecar_client,
        base_env={"PATH": os.environ["PATH"]},
    )
    local_spy = Mock(wraps=manager._run_locally)
    manager._run_locally = local_spy  # type: ignore[method-assign]

    result = await ExecutableUnitRunner(
        registry, sandbox_manager=manager, project_materializer=lambda _m, _c: None
    ).run_manifest_ref("eu://sidecar-unit", {"name": "x", "count": 1})

    assert result.output_data == {"answer": "sidecar", "score": 1}
    assert result.sandbox_result.backend == "sidecar"
    sidecar_client.execute.assert_awaited_once()
    local_spy.assert_not_called()  # the previously-broken host-execution path
