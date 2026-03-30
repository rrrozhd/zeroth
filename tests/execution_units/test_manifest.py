from __future__ import annotations

import pytest

from zeroth.execution_units import (
    BuildConfig,
    CommandArtifactSource,
    ExecutableUnitValidator,
    ExecutionMode,
    InputMode,
    ManifestValidationError,
    NativeUnitManifest,
    OutputMode,
    ProjectArchiveArtifactSource,
    ProjectUnitManifest,
    PythonModuleArtifactSource,
    ResourceLimits,
    RunConfig,
    WrappedCommandUnitManifest,
)


def test_manifest_validator_accepts_each_onboarding_mode() -> None:
    validator = ExecutableUnitValidator()

    native = NativeUnitManifest(
        unit_id="native-unit",
        version=1,
        onboarding_mode=ExecutionMode.NATIVE,
        runtime="python",
        artifact_source=PythonModuleArtifactSource(
            ref="demo.native:handler",
        ),
        callable_ref="demo.native:handler",
        entrypoint_type="python_callable",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        resource_limits=ResourceLimits(timeout_seconds=5),
        timeout_seconds=5,
        cache_identity_fields={"python": "3.12"},
        metadata={"description": "native"},
    )
    wrapped = WrappedCommandUnitManifest(
        unit_id="wrapped-unit",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref="rust-binary"),
        entrypoint_type="command",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(command=["echo", "{}"], working_directory="/tmp"),
        cache_identity_fields={"binary": "echo"},
        side_effect=True,
    )
    project = ProjectUnitManifest(
        unit_id="project-unit",
        onboarding_mode=ExecutionMode.PROJECT,
        runtime="project",
        artifact_source=ProjectArchiveArtifactSource(ref="archive://project.tar"),
        build_config=BuildConfig(command=["python", "-m", "build"]),
        run_config=RunConfig(command=["python", "-m", "project.entry"]),
        project_archive_ref="archive://project.tar",
        entrypoint_type="project",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        cache_identity_fields={"archive": "project.tar"},
    )

    assert validator.validate_or_raise(native).is_valid
    assert validator.validate_or_raise(wrapped).is_valid
    assert validator.validate_or_raise(project).is_valid


def test_manifest_validator_rejects_missing_required_wrapped_command() -> None:
    manifest = WrappedCommandUnitManifest(
        unit_id="wrapped-unit",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref="rust-binary"),
        entrypoint_type="command",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(),
    )

    with pytest.raises(ManifestValidationError) as exc_info:
        ExecutableUnitValidator().validate_or_raise(manifest)

    assert exc_info.value.report.errors
    assert exc_info.value.report.summary()["errors"] == 1
