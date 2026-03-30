from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from zeroth.execution_units import (
    CommandArtifactSource,
    CommandRuntimeAdapter,
    ExecutionMode,
    InputMode,
    NativeUnitManifest,
    OutputMode,
    PythonModuleArtifactSource,
    PythonRuntimeAdapter,
    RunConfig,
    WrappedCommandUnitManifest,
)


class ExampleInput(BaseModel):
    value: int


class ExampleOutput(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_python_runtime_adapter_materializes_and_executes_tool() -> None:
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

    adapter = PythonRuntimeAdapter()

    async def handler(ctx, data):  # noqa: ANN001, ARG001
        return {"value": data.value + 1}

    tool = adapter.materialize(
        manifest,
        input_model=ExampleInput,
        output_model=ExampleOutput,
        handler=handler,
    )
    result = await tool.execute(None, ExampleInput(value=2))

    assert type(tool).__name__ == "PythonTool"
    assert result == ExampleOutput(value=3)


@pytest.mark.asyncio
async def test_command_runtime_adapter_materializes_and_executes_cli_tool(tmp_path: Path) -> None:
    script = tmp_path / "double.py"
    script.write_text(
        """
import json
import sys

payload = json.load(sys.stdin)
payload["value"] *= 2
print(json.dumps(payload))
""".strip()
    )

    manifest = WrappedCommandUnitManifest(
        unit_id="command-unit",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref=str(script)),
        entrypoint_type="command",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(command=[sys.executable, str(script)]),
        cache_identity_fields={"script": script.name},
    )

    adapter = CommandRuntimeAdapter()
    tool = adapter.materialize(
        manifest,
        input_model=ExampleInput,
        output_model=ExampleOutput,
    )
    result = await tool.execute(None, ExampleInput(value=4))

    assert type(tool).__name__ in {"CLITool", "PythonTool"}
    assert result == ExampleOutput(value=8)
