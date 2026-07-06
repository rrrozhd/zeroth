from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from zeroth.core.execution_units.models import (
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
from zeroth.core.execution_units.runner import (
    ExecutableUnitBinding,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
)


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
    runner = ExecutableUnitRunner(registry)

    first = await runner.run_manifest_ref("eu://project-unit", {"name": "gamma", "count": 5})
    second = await runner.run_manifest_ref("eu://project-unit", {"name": "delta", "count": 6})

    assert first.output_data == {"answer": "gamma", "score": 5}
    assert second.output_data == {"answer": "delta", "score": 6}
    assert build_marker.read_text() == "1"


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
