"""Registry and runner for executing units with sandboxing and I/O handling.

This module ties everything together: you register executable unit bindings
(manifest + models + handler) in a registry, then use the runner to actually
execute them. The runner handles input validation, sandboxing, subprocess
management, and output extraction.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any

from governai.tools.python_tool import PythonHandler
from pydantic import BaseModel, ValidationError

from zeroth.execution_units.adapters import PythonRuntimeAdapter
from zeroth.execution_units.io import ExtractedOutput, convert_output, extract_output, inject_input
from zeroth.execution_units.models import (
    ExecutableUnitManifest,
    NativeUnitManifest,
    ProjectUnitManifest,
    WrappedCommandUnitManifest,
)
from zeroth.execution_units.sandbox import (
    SandboxExecutionResult,
    SandboxManager,
    SandboxTimeoutError,
)

_DEFAULT_ALLOWED_ENV_KEYS = ("PATH", "PYTHONPATH", "HOME", "TMPDIR", "TMP", "TEMP")


class ExecutableUnitError(RuntimeError):
    """Base error for anything that goes wrong when running an executable unit."""


class ExecutableUnitNotFoundError(ExecutableUnitError):
    """Raised when you try to run a manifest ref that has not been registered."""


class ExecutableUnitExecutionError(ExecutableUnitError):
    """Raised when an executable unit crashes or fails during build or run."""


class ExecutableUnitInputError(ExecutableUnitExecutionError):
    """Raised when the input data does not match the expected schema."""


@dataclass(frozen=True, slots=True)
class ExecutableUnitBinding:
    """Links a manifest to its input/output models and optional Python handler.

    This is what you register in the ExecutableUnitRegistry. It bundles
    together everything the runner needs to execute a particular unit.
    """

    manifest_ref: str
    manifest: ExecutableUnitManifest
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    python_handler: PythonHandler | None = None
    allowed_env_keys: tuple[str, ...] = _DEFAULT_ALLOWED_ENV_KEYS
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutableUnitRunResult:
    """The result of running an executable unit.

    Contains the input that was sent, the output that came back, and
    details about the sandbox execution and audit trail.
    """

    manifest_ref: str
    input_data: dict[str, Any]
    output_data: dict[str, Any]
    sandbox_result: SandboxExecutionResult | None = None
    extracted_output: ExtractedOutput | None = None
    audit_record: dict[str, Any] = field(default_factory=dict)


class ExecutableUnitRegistry:
    """A lookup table that maps manifest ref strings to their bindings.

    Register bindings here so the runner can find them by name later.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, ExecutableUnitBinding] = {}

    def register(
        self,
        binding: ExecutableUnitBinding | str,
        manifest: ExecutableUnitManifest | None = None,
        *,
        input_model: type[BaseModel] | None = None,
        output_model: type[BaseModel] | None = None,
        handler: PythonHandler | None = None,
        allowed_env_keys: Sequence[str] = _DEFAULT_ALLOWED_ENV_KEYS,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutableUnitBinding:
        """Add an executable unit binding to the registry.

        You can pass a pre-built ExecutableUnitBinding, or pass a ref string
        along with a manifest and models to build one automatically.
        """
        if isinstance(binding, ExecutableUnitBinding):
            resolved = binding
        else:
            if manifest is None or input_model is None or output_model is None:
                raise ValueError(
                    "manifest, input_model, and output_model are required when binding is a ref"
                )
            resolved = ExecutableUnitBinding(
                manifest_ref=binding,
                manifest=manifest,
                input_model=input_model,
                output_model=output_model,
                python_handler=handler,
                allowed_env_keys=tuple(allowed_env_keys),
                metadata=dict(metadata or {}),
            )
        existing = self._bindings.get(resolved.manifest_ref)
        if existing is not None and existing != resolved:
            raise ValueError(f"duplicate manifest_ref: {resolved.manifest_ref}")
        self._bindings[resolved.manifest_ref] = resolved
        return resolved

    def get(self, manifest_ref: str) -> ExecutableUnitBinding:
        """Look up a binding by its ref string. Raises if not found."""
        try:
            return self._bindings[manifest_ref]
        except KeyError as exc:
            raise ExecutableUnitNotFoundError(manifest_ref) from exc

    def has(self, manifest_ref: str) -> bool:
        """Return True if a binding with this ref string exists."""
        return manifest_ref in self._bindings


class ExecutableUnitRunner:
    """The main class that actually runs executable units.

    It looks up bindings from the registry, validates input, runs the unit
    (either as a Python function or as a sandboxed subprocess), extracts
    the output, and returns a structured result.
    """

    def __init__(
        self,
        registry: ExecutableUnitRegistry | None = None,
        *,
        sandbox_manager: SandboxManager | None = None,
        python_adapter: PythonRuntimeAdapter | None = None,
    ) -> None:
        self.registry = registry or ExecutableUnitRegistry()
        self.sandbox_manager = sandbox_manager or SandboxManager()
        self.python_adapter = python_adapter or PythonRuntimeAdapter()
        self._built_cache_keys: set[str] = set()

    async def run_manifest_ref(
        self,
        manifest_ref: str,
        payload: BaseModel | Mapping[str, Any],
    ) -> ExecutableUnitRunResult:
        """Run an executable unit by looking it up in the registry by ref string."""
        return await self.run_binding(self.registry.get(manifest_ref), payload)

    async def run(
        self,
        manifest_ref: str,
        payload: BaseModel | Mapping[str, Any],
    ) -> ExecutableUnitRunResult:
        """Shortcut for run_manifest_ref. Run a unit by its ref string."""
        return await self.run_manifest_ref(manifest_ref, payload)

    async def run_binding(
        self,
        binding: ExecutableUnitBinding,
        payload: BaseModel | Mapping[str, Any],
    ) -> ExecutableUnitRunResult:
        """Run an executable unit from a binding directly.

        Validates the input, then dispatches to either native Python execution
        or sandboxed subprocess execution depending on the manifest type.
        """
        validated_input = self._validate_input(binding.input_model, payload)
        input_data = validated_input.model_dump(mode="json")
        manifest = binding.manifest

        if isinstance(manifest, NativeUnitManifest):
            output_model = await self._run_native(binding, validated_input)
            return ExecutableUnitRunResult(
                manifest_ref=binding.manifest_ref,
                input_data=input_data,
                output_data=output_model.model_dump(mode="json"),
                audit_record={
                    "manifest_ref": binding.manifest_ref,
                    "runtime": manifest.runtime.value,
                    "execution_mode": manifest.onboarding_mode.value,
                    "sandboxed": False,
                },
            )

        return await self._run_subprocess_unit(binding, validated_input)

    def _validate_input(
        self,
        input_model: type[BaseModel],
        payload: BaseModel | Mapping[str, Any],
    ) -> BaseModel:
        """Check that the payload matches the expected input schema."""
        try:
            if isinstance(payload, BaseModel):
                return input_model.model_validate(payload.model_dump(mode="json"))
            return input_model.model_validate(payload)
        except ValidationError as exc:
            raise ExecutableUnitInputError(f"input validation failed: {exc}") from exc

    async def _run_native(
        self,
        binding: ExecutableUnitBinding,
        validated_input: BaseModel,
    ) -> BaseModel:
        """Run a native Python unit by calling its handler function directly."""
        if binding.python_handler is None:
            raise ExecutableUnitExecutionError(
                f"native manifest {binding.manifest_ref} requires a python handler"
            )
        tool = self.python_adapter.materialize(
            binding.manifest,
            input_model=binding.input_model,
            output_model=binding.output_model,
            handler=binding.python_handler,
        )
        return await tool.execute(None, validated_input)

    async def _run_subprocess_unit(
        self,
        binding: ExecutableUnitBinding,
        validated_input: BaseModel,
    ) -> ExecutableUnitRunResult:
        """Run a command or project unit as a sandboxed subprocess.

        Sets up a temporary directory, injects input, runs the command,
        extracts and validates the output, and returns the result.
        """
        manifest = binding.manifest
        environment = self.sandbox_manager.prepare_environment(
            allowed_env_keys=binding.allowed_env_keys,
            overlay=manifest.run_config.environment,
            cache_identity=manifest.cache_identity_fields,
            runtime=manifest.runtime.value,
            runtime_version=str(manifest.version),
            dependency_manifest=[
                dependency.model_dump(mode="json") for dependency in manifest.dependencies
            ],
            build_config=(
                manifest.build_config.model_dump(mode="json")
                if manifest.build_config is not None
                else None
            ),
            sandbox_policy=manifest.resource_limits.model_dump(mode="json"),
        )

        with tempfile.TemporaryDirectory(prefix="zeroth-eu-") as tempdir:
            sandbox_root = Path(tempdir)
            cwd = self._resolve_workdir(sandbox_root, manifest.run_config.working_directory)
            input_file = cwd / "zeroth-input.json"
            output_file = cwd / "zeroth-output.json"
            injected = inject_input(
                manifest.input_mode,
                validated_input,
                input_file_path=input_file,
            )
            overlay_env = dict(environment.variables)
            overlay_env.update(injected.env)
            if manifest.output_mode.value == "output_file_json":
                overlay_env["ZEROTH_OUTPUT_FILE"] = str(output_file)

            build_cache_key = environment.cache_key
            if isinstance(manifest, ProjectUnitManifest):
                await self._maybe_build_project(
                    manifest,
                    cwd=cwd,
                    base_env=overlay_env,
                    build_cache_key=build_cache_key,
                )

            sandbox_result = await self._execute_command(
                self._command_for(manifest, injected.argv),
                cwd=cwd,
                env=overlay_env,
                input_text=injected.stdin,
                timeout_seconds=manifest.timeout_seconds,
            )
            if sandbox_result.returncode != 0 and manifest.output_mode.value != "exit_code_only":
                raise ExecutableUnitExecutionError(
                    f"command failed for {binding.manifest_ref} with exit code "
                    f"{sandbox_result.returncode}: {sandbox_result.stderr or sandbox_result.stdout}"
                )

            extracted_output = extract_output(
                manifest.output_mode,
                stdout=sandbox_result.stdout,
                stderr=sandbox_result.stderr,
                exit_code=sandbox_result.returncode,
                output_file_path=(
                    output_file if manifest.output_mode.value == "output_file_json" else None
                ),
            )
            converted = convert_output(binding.output_model, extracted_output)
            return ExecutableUnitRunResult(
                manifest_ref=binding.manifest_ref,
                input_data=validated_input.model_dump(mode="json"),
                output_data=converted.model_dump(mode="json"),
                sandbox_result=sandbox_result,
                extracted_output=extracted_output,
                audit_record={
                    "manifest_ref": binding.manifest_ref,
                    "runtime": manifest.runtime.value,
                    "execution_mode": manifest.onboarding_mode.value,
                    "cache_key": build_cache_key,
                    "sandboxed": True,
                    "backend": sandbox_result.backend,
                },
            )

    def _resolve_workdir(self, sandbox_root: Path, working_directory: str | None) -> Path:
        """Determine the working directory inside the sandbox, creating it if needed."""
        if working_directory is None:
            cwd = sandbox_root
        else:
            relative = Path(working_directory)
            if relative.is_absolute():
                raise ExecutableUnitExecutionError(
                    "run_config.working_directory must be relative to the sandbox root"
                )
            cwd = sandbox_root / relative
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd

    async def _maybe_build_project(
        self,
        manifest: ProjectUnitManifest,
        *,
        cwd: Path,
        base_env: dict[str, str],
        build_cache_key: str,
    ) -> None:
        """Run the project's build command if it has not been built yet."""
        if not manifest.build_config.command or build_cache_key in self._built_cache_keys:
            return
        build_env = dict(base_env)
        build_env.update(manifest.build_config.environment)
        result = await self._execute_command(
            manifest.build_config.command,
            cwd=cwd,
            env=build_env,
            timeout_seconds=manifest.timeout_seconds,
        )
        if result.returncode != 0:
            raise ExecutableUnitExecutionError(
                f"build failed for {manifest.unit_id} with exit code {result.returncode}: "
                f"{result.stderr or result.stdout}"
            )
        self._built_cache_keys.add(build_cache_key)

    async def _execute_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SandboxExecutionResult:
        """Run a command as an async subprocess with optional stdin and timeout."""
        started_at = perf_counter()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if input_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=dict(env),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(None if input_text is None else input_text.encode("utf-8")),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            stdout, stderr = await process.communicate()
            raise SandboxTimeoutError(
                command=command,
                timeout_seconds=timeout_seconds,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
            ) from exc

        return SandboxExecutionResult(
            command=tuple(command),
            returncode=process.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            workdir=str(cwd),
            environment=dict(env),
            duration_seconds=perf_counter() - started_at,
            cache_key=None,
        )

    def _command_for(
        self,
        manifest: WrappedCommandUnitManifest | ProjectUnitManifest,
        argv: Sequence[str] = (),
    ) -> list[str]:
        """Build the full command list from the manifest's config plus extra args."""
        if manifest.run_config.command:
            command = list(manifest.run_config.command)
        else:
            command = [manifest.artifact_source.ref]
        command.extend(argv)
        return command


__all__ = [
    "ExecutableUnitBinding",
    "ExecutableUnitExecutionError",
    "ExecutableUnitInputError",
    "ExecutableUnitNotFoundError",
    "ExecutableUnitRegistry",
    "ExecutableUnitRunResult",
    "ExecutableUnitRunner",
]
