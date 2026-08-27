"""Evaluation-only bindings for local, manifest-backed sandbox code."""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from zeroth.integrations.execution import (
    CommandArtifactSource,
    ExecutionMode,
    InputMode,
    OutputMode,
    RunConfig,
    WrappedCommandUnitManifest,
)
from zeroth.integrations.execution.runner import ExecutableUnitRunner

LOCAL_RECORD_PROFILER_MANIFEST_REF = "evaluation://local-code/record-profiler/v1"


class LocalRecordProfileInput(BaseModel):
    """Synthetic records and the fields required for a complete record."""

    model_config = ConfigDict(extra="forbid")

    records: list[dict[str, object]] = Field(min_length=1, max_length=100)
    required_fields: list[str] = Field(min_length=1, max_length=32)


class LocalRecordProfileOutput(BaseModel):
    """Deterministic completeness profile emitted by the local code unit."""

    model_config = ConfigDict(extra="forbid")

    total_records: int = Field(ge=1)
    missing_counts: dict[str, int]
    complete_records: int = Field(ge=0)
    completeness_pct: float = Field(ge=0, le=100)
    ready: bool


class LocalLoopPayload(BaseModel):
    """Contract-preserving payload for deterministic loop transformations."""

    model_config = ConfigDict(extra="allow")


_LOOP_OPERATIONS = (
    "incident-assess",
    "incident-prepare",
    "incident-finalize",
    "incident-escalate",
    "quality-inspect",
    "quality-repair",
    "quality-finalize",
    "quality-manual-review",
)


def register_local_code_manifests(runner: ExecutableUnitRunner) -> None:
    """Register code whose artifact is pinned to this repository checkout."""
    repository_root = Path(__file__).resolve().parents[2]
    script = (
        repository_root / "release" / "live_evaluation" / "code_units" / "record_profiler.py"
    ).resolve(strict=True)
    script.relative_to(repository_root)
    runner.registry.register(
        LOCAL_RECORD_PROFILER_MANIFEST_REF,
        WrappedCommandUnitManifest(
            unit_id="evaluation-local-record-profiler",
            onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
            runtime="command",
            artifact_source=CommandArtifactSource(ref=str(script)),
            run_config=RunConfig(command=[sys.executable, str(script)]),
            entrypoint_type="command",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="evaluation://local-code/record-profiler/input/v1",
            output_contract_ref="evaluation://local-code/record-profiler/output/v1",
            timeout_seconds=5,
            side_effect=False,
            metadata={
                "description": "Profile synthetic record completeness from local code",
                "evaluation_only": True,
                "external_calls": False,
                "source_kind": "repository_file",
            },
        ),
        input_model=LocalRecordProfileInput,
        output_model=LocalRecordProfileOutput,
        metadata={"description": "Local sandboxed record completeness profiler"},
    )
    loop_script = (
        repository_root / "release" / "live_evaluation" / "code_units" / "loop_demo.py"
    ).resolve(strict=True)
    loop_script.relative_to(repository_root)
    for operation in _LOOP_OPERATIONS:
        manifest_ref = f"evaluation://local-code/{operation}/v1"
        runner.registry.register(
            manifest_ref,
            WrappedCommandUnitManifest(
                unit_id=f"evaluation-local-{operation}",
                onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
                runtime="command",
                artifact_source=CommandArtifactSource(ref=str(loop_script)),
                run_config=RunConfig(command=[sys.executable, str(loop_script), operation]),
                entrypoint_type="command",
                input_mode=InputMode.JSON_STDIN,
                output_mode=OutputMode.JSON_STDOUT,
                input_contract_ref=f"evaluation://local-code/{operation}/input/v1",
                output_contract_ref=f"evaluation://local-code/{operation}/output/v1",
                timeout_seconds=12 if operation == "quality-inspect" else 5,
                side_effect=False,
                metadata={
                    "description": f"Deterministic local loop step: {operation}",
                    "evaluation_only": True,
                    "external_calls": False,
                    "source_kind": "repository_file",
                },
            ),
            input_model=LocalLoopPayload,
            output_model=LocalLoopPayload,
            metadata={"description": f"Local sandboxed loop transformation: {operation}"},
        )


__all__ = [
    "LOCAL_RECORD_PROFILER_MANIFEST_REF",
    "LocalRecordProfileInput",
    "LocalRecordProfileOutput",
    "LocalLoopPayload",
    "register_local_code_manifests",
]
