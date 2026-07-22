"""Canonical import surface for the execution integrations package.

Non-golden boundary tests for the Task 15 execution-units move: the
canonical ``zeroth.integrations.execution`` package must publish the same
objects the legacy ``zeroth.core.execution_units`` path keeps republishing,
and both packages must stay cold-importable from a fresh interpreter in
either order.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

EXPORTS = (
    "AdmissionController",
    "AdmissionResult",
    "ArtifactSource",
    "AuditSettings",
    "BuildConfig",
    "CommandArtifactSource",
    "CommandRuntimeAdapter",
    "DependencySpec",
    "DockerSandboxConfig",
    "EntryPointType",
    "EnvironmentCacheManager",
    "EnvironmentVariable",
    "ExecutableUnitAdmissionError",
    "ExecutableUnitBinding",
    "ExecutableUnitError",
    "ExecutableUnitExecutionError",
    "ExecutableUnitManifest",
    "ExecutableUnitNotFoundError",
    "ExecutableUnitRegistry",
    "ExecutableUnitRunResult",
    "ExecutableUnitRunner",
    "ExecutableUnitValidator",
    "ExecutionIOError",
    "ExecutionMode",
    "ExtractedOutput",
    "FreeformPayload",
    "InjectedInput",
    "InlineSourceArtifactSource",
    "InlineUnitManifest",
    "InputInjectionError",
    "InputMode",
    "ManifestIntegrityRecord",
    "ManifestValidationError",
    "NativeUnitManifest",
    "OutputConversionError",
    "OutputExtractionError",
    "OutputMode",
    "ProjectArchiveArtifactSource",
    "ProjectUnitManifest",
    "PythonModuleArtifactSource",
    "PythonRuntimeAdapter",
    "ResourceConstraints",
    "ResourceLimits",
    "RunConfig",
    "RuntimeAdapter",
    "RuntimeLanguage",
    "SandboxBackendMode",
    "SandboxBackendUnavailableError",
    "SandboxConfig",
    "SandboxEnvironment",
    "SandboxExecutionResult",
    "SandboxManager",
    "SandboxPolicyViolationError",
    "SandboxStrictnessMode",
    "SandboxTimeoutError",
    "ValidationCode",
    "WrappedCommandUnitManifest",
    "build_docker_resource_flags",
    "build_inline_binding",
    "build_inline_manifest",
    "build_sandbox_environment",
    "compute_environment_cache_key",
    "compute_manifest_digest",
    "convert_output",
    "docker_container_running",
    "extract_output",
    "inject_input",
    "inline_source_digest",
)


def test_execution_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core import execution_units as legacy
    from zeroth.integrations import execution as canonical

    for name in EXPORTS:
        assert getattr(canonical, name) is getattr(legacy, name), name


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        ("adapters", ("CommandRuntimeAdapter", "PythonRuntimeAdapter", "RuntimeAdapter")),
        ("constraints", ("ResourceConstraints", "build_docker_resource_flags")),
        ("errors", ("ManifestValidationError",)),
        (
            "inline",
            (
                "FreeformPayload",
                "build_inline_binding",
                "build_inline_manifest",
                "inline_source_digest",
            ),
        ),
        (
            "integrity",
            (
                "AdmissionController",
                "AdmissionResult",
                "ManifestIntegrityRecord",
                "compute_manifest_digest",
            ),
        ),
        (
            "io",
            (
                "ExecutionIOError",
                "ExtractedOutput",
                "InjectedInput",
                "InputInjectionError",
                "OutputConversionError",
                "OutputExtractionError",
                "convert_output",
                "extract_output",
                "inject_input",
            ),
        ),
        (
            "models",
            (
                "EnvironmentVariable",
                "ExecutableUnitManifest",
                "ExecutionMode",
                "InlineUnitManifest",
                "NativeUnitManifest",
                "ResourceLimits",
                "RunConfig",
            ),
        ),
        (
            "runner",
            (
                "ExecutableUnitBinding",
                "ExecutableUnitRegistry",
                "ExecutableUnitRunResult",
                "ExecutableUnitRunner",
            ),
        ),
        (
            "sandbox",
            (
                "DockerSandboxConfig",
                "SandboxConfig",
                "SandboxEnvironment",
                "SandboxManager",
                "build_sandbox_environment",
            ),
        ),
        ("sidecar_client", ("SandboxSidecarClient",)),
        ("validator", ("ExecutableUnitValidator", "ValidationCode")),
    ],
)
def test_execution_modules_are_the_same_surface_through_both_paths(
    module_name: str, names: tuple[str, ...]
) -> None:
    import importlib

    legacy_module = importlib.import_module(f"zeroth.core.execution_units.{module_name}")
    canonical_module = importlib.import_module(f"zeroth.integrations.execution.{module_name}")

    for name in names:
        assert getattr(canonical_module, name) is getattr(legacy_module, name), name


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("zeroth.integrations.execution", "zeroth.core.execution_units"),
        ("zeroth.core.execution_units", "zeroth.integrations.execution"),
    ],
)
def test_execution_cold_imports_from_both_directions(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}\nimport {second}\n"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"cold import {first} then {second} failed:\n{result.stderr}"
