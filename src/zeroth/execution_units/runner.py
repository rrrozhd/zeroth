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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from governai.tools.python_tool import PythonHandler
from pydantic import BaseModel, ValidationError

from zeroth.execution_units.adapters import PythonRuntimeAdapter
from zeroth.execution_units.constraints import ResourceConstraints
from zeroth.execution_units.integrity import AdmissionController
from zeroth.execution_units.io import (
    ExtractedOutput,
    convert_output,
    extract_output,
    inject_input,
)
from zeroth.execution_units.models import (
    ExecutableUnitManifest,
    NativeUnitManifest,
    ProjectUnitManifest,
    WrappedCommandUnitManifest,
)
from zeroth.execution_units.sandbox import (
    SandboxEnvironment,
    SandboxExecutionResult,
    SandboxManager,
    SandboxStrictnessMode,
)
from zeroth.policy import Capability, apply_secret_policy
from zeroth.secrets import SecretResolver

_DEFAULT_ALLOWED_ENV_KEYS = ("PATH", "PYTHONPATH", "HOME", "TMPDIR", "TMP", "TEMP")


class ExecutableUnitError(RuntimeError):
    """Base error for anything that goes wrong when running an executable unit."""


class ExecutableUnitNotFoundError(ExecutableUnitError):
    """Raised when you try to run a manifest ref that has not been registered."""


class ExecutableUnitExecutionError(ExecutableUnitError):
    """Raised when an executable unit crashes or fails during build or run."""


class ExecutableUnitInputError(ExecutableUnitExecutionError):
    """Raised when the input data does not match the expected schema."""


class ExecutableUnitAdmissionError(ExecutableUnitExecutionError):
    """Raised when an executable unit fails admission control before execution."""

    def __init__(self, message: str, *, audit_record: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.audit_record = dict(audit_record or {})


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
        secret_resolver: SecretResolver | None = None,
        admission_controller: AdmissionController | None = None,
    ) -> None:
        self.registry = registry or ExecutableUnitRegistry()
        self.sandbox_manager = sandbox_manager or SandboxManager()
        self.python_adapter = python_adapter or PythonRuntimeAdapter()
        self.secret_resolver = secret_resolver
        self.admission_controller = admission_controller
        self._built_cache_keys: set[str] = set()

    async def run_manifest_ref(
        self,
        manifest_ref: str,
        payload: BaseModel | Mapping[str, Any],
        *,
        enforcement_context: Mapping[str, Any] | None = None,
    ) -> ExecutableUnitRunResult:
        """Run an executable unit by looking it up in the registry by ref string."""
        return await self.run_binding(
            self.registry.get(manifest_ref),
            payload,
            enforcement_context=enforcement_context,
        )

    async def run(
        self,
        manifest_ref: str,
        payload: BaseModel | Mapping[str, Any],
        *,
        enforcement_context: Mapping[str, Any] | None = None,
    ) -> ExecutableUnitRunResult:
        """Shortcut for run_manifest_ref. Run a unit by its ref string."""
        return await self.run_manifest_ref(
            manifest_ref,
            payload,
            enforcement_context=enforcement_context,
        )

    async def run_binding(
        self,
        binding: ExecutableUnitBinding,
        payload: BaseModel | Mapping[str, Any],
        *,
        enforcement_context: Mapping[str, Any] | None = None,
    ) -> ExecutableUnitRunResult:
        """Run an executable unit from a binding directly.

        Validates the input, then dispatches to either native Python execution
        or sandboxed subprocess execution depending on the manifest type.
        """
        validated_input = self._validate_input(binding.input_model, payload)
        input_data = validated_input.model_dump(mode="json")
        manifest = binding.manifest
        self._admit_manifest(binding)

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

        return await self._run_subprocess_unit(
            binding,
            validated_input,
            enforcement_context=enforcement_context,
        )

    def _admit_manifest(self, binding: ExecutableUnitBinding) -> None:
        """Reject untrusted manifests before any execution begins."""
        controller = self.admission_controller
        if controller is None:
            return
        result = controller.admit(binding.manifest)
        if result.admitted:
            return
        raise ExecutableUnitAdmissionError(
            f"admission denied for {binding.manifest_ref}: {result.reason}",
            audit_record={
                "admission": {
                    "admitted": result.admitted,
                    "reason": result.reason,
                    "digest": result.digest,
                }
            },
        )

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
        *,
        enforcement_context: Mapping[str, Any] | None = None,
    ) -> ExecutableUnitRunResult:
        """Run a command or project unit as a sandboxed subprocess.

        Sets up a temporary directory, injects input, runs the command,
        extracts and validates the output, and returns the result.
        """
        manifest = binding.manifest
        enforcement = dict(enforcement_context or {})
        manifest_env, secret_env_keys = self._manifest_environment(manifest)
        secret_filtered_env = self._apply_allowed_secrets(
            manifest_env,
            enforcement,
            secret_env_keys=secret_env_keys,
        )
        environment = None
        if hasattr(self.sandbox_manager, "prepare_environment"):
            environment = self.sandbox_manager.prepare_environment(
                allowed_env_keys=binding.allowed_env_keys,
                overlay=secret_filtered_env,
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
            overlay_env = dict(secret_filtered_env)
            overlay_env.update(injected.env)
            if manifest.output_mode.value == "output_file_json":
                overlay_env["ZEROTH_OUTPUT_FILE"] = str(output_file)
            timeout_seconds = self._effective_timeout(
                manifest.timeout_seconds,
                enforcement.get("timeout_override_seconds"),
            )
            resource_constraints = self._resource_constraints_for(manifest, enforcement)
            sandbox_strictness_mode = self._sandbox_strictness_mode_for(enforcement)

            build_cache_key = environment.cache_key if environment is not None else None
            if isinstance(manifest, ProjectUnitManifest):
                await self._maybe_build_project(
                    manifest,
                    cwd=cwd,
                    sandbox_root=sandbox_root,
                    relative_cwd=cwd.relative_to(sandbox_root),
                    base_env=overlay_env,
                    build_cache_key=build_cache_key,
                    timeout_seconds=timeout_seconds,
                    resource_constraints=resource_constraints,
                    sandbox_strictness_mode=sandbox_strictness_mode,
                )

            sandbox_result = await self._execute_command(
                self._command_for(manifest, injected.argv),
                cwd=cwd,
                sandbox_root=sandbox_root,
                relative_cwd=cwd.relative_to(sandbox_root),
                allowed_env_keys=binding.allowed_env_keys,
                overlay_env=overlay_env,
                input_text=injected.stdin,
                timeout_seconds=timeout_seconds,
                resource_constraints=resource_constraints,
                sandbox_strictness_mode=sandbox_strictness_mode,
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
                    "enforcement": dict(enforcement),
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
        sandbox_root: Path,
        relative_cwd: Path,
        base_env: dict[str, str],
        build_cache_key: str,
        timeout_seconds: float | None,
        resource_constraints: ResourceConstraints | None,
        sandbox_strictness_mode: SandboxStrictnessMode | None,
    ) -> None:
        """Run the project's build command if it has not been built yet."""
        if not manifest.build_config.command or build_cache_key in self._built_cache_keys:
            return
        build_env = dict(base_env)
        build_env.update(manifest.build_config.environment)
        result = await self._execute_command(
            manifest.build_config.command,
            cwd=cwd,
            sandbox_root=sandbox_root,
            relative_cwd=relative_cwd,
            allowed_env_keys=None,
            overlay_env=build_env,
            timeout_seconds=timeout_seconds,
            resource_constraints=resource_constraints,
            sandbox_strictness_mode=sandbox_strictness_mode,
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
        sandbox_root: Path,
        relative_cwd: Path | None,
        allowed_env_keys: Sequence[str] | None,
        overlay_env: Mapping[str, str],
        input_text: str | None = None,
        timeout_seconds: float | None = None,
        resource_constraints: ResourceConstraints | None = None,
        sandbox_strictness_mode: SandboxStrictnessMode | None = None,
    ) -> SandboxExecutionResult:
        """Run a command through the sandbox manager with optional policy overrides."""
        sandbox_manager = self._sandbox_manager_with_strictness(sandbox_strictness_mode)
        if hasattr(sandbox_manager, "_resolve_backend") and hasattr(
            sandbox_manager,
            "_run_locally",
        ):
            environment = sandbox_manager.prepare_environment(
                allowed_env_keys=allowed_env_keys,
                overlay=overlay_env,
            )
            return await asyncio.to_thread(
                self._run_with_prepared_environment,
                sandbox_manager,
                command,
                cwd,
                sandbox_root,
                relative_cwd,
                environment,
                input_text,
                timeout_seconds,
                resource_constraints,
            )
        return await asyncio.to_thread(
            sandbox_manager.run,
            command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            allowed_env_keys=allowed_env_keys,
            overlay_env=overlay_env,
            resource_constraints=resource_constraints,
        )

    def _sandbox_manager_with_strictness(
        self,
        sandbox_strictness_mode: SandboxStrictnessMode | None,
    ) -> SandboxManager:
        """Return a sandbox manager instance with the requested strictness override."""
        if sandbox_strictness_mode is None or not hasattr(self.sandbox_manager, "_config"):
            return self.sandbox_manager
        config = self.sandbox_manager._config  # noqa: SLF001
        if config.strictness_mode is sandbox_strictness_mode:
            return self.sandbox_manager
        return SandboxManager(
            base_env=getattr(self.sandbox_manager, "_base_env", None),
            cache_manager=self.sandbox_manager.cache_manager,
            config=replace(config, strictness_mode=sandbox_strictness_mode),
            command_runner=getattr(self.sandbox_manager, "_command_runner", None),
            container_inspector=getattr(self.sandbox_manager, "_container_inspector", None),
        )

    def _run_with_prepared_environment(
        self,
        sandbox_manager: SandboxManager,
        command: Sequence[str],
        cwd: Path,
        sandbox_root: Path,
        relative_cwd: Path | None,
        environment: SandboxEnvironment,
        input_text: str | None,
        timeout_seconds: float | None,
        resource_constraints: ResourceConstraints | None,
    ) -> SandboxExecutionResult:
        """Dispatch execution through SandboxManager internals using a prepared sandbox root."""
        backend = sandbox_manager._resolve_backend(resource_constraints)  # noqa: SLF001
        if backend.value == "docker":
            return sandbox_manager._run_in_docker(  # noqa: SLF001
                command=command,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
                sandbox_root=sandbox_root,
                relative_cwd=relative_cwd,
                environment=environment,
                resource_constraints=resource_constraints,
            )
        return sandbox_manager._run_locally(  # noqa: SLF001
            command=command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            environment=environment,
            resource_constraints=resource_constraints,
        )

    def _apply_allowed_secrets(
        self,
        environment: Mapping[str, str],
        enforcement_context: Mapping[str, Any],
        *,
        secret_env_keys: set[str],
    ) -> dict[str, str]:
        """Filter environment variables using the policy-derived secret allowlist."""
        if not secret_env_keys:
            return dict(environment)
        capabilities = enforcement_context.get("effective_capabilities") or set()
        normalized_capabilities = {
            capability.value if isinstance(capability, Capability) else str(capability)
            for capability in capabilities
        }
        preserved_environment = {
            key: value for key, value in environment.items() if key not in secret_env_keys
        }
        filtered_secret_env = apply_secret_policy(
            {key: value for key, value in environment.items() if key in secret_env_keys},
            allowed_secrets=list(enforcement_context.get("allowed_secrets") or []),
            secret_access_enabled=Capability.SECRET_ACCESS.value in normalized_capabilities,
        )
        preserved_environment.update(filtered_secret_env)
        return preserved_environment

    def _manifest_environment(
        self,
        manifest: ExecutableUnitManifest,
    ) -> tuple[dict[str, str], set[str]]:
        """Build the manifest-defined environment, resolving secret refs when needed."""
        environment = dict(manifest.run_config.environment)
        secret_env_keys: set[str] = set()
        if not manifest.environment_variables:
            return environment, secret_env_keys
        resolver = self.secret_resolver
        if resolver is None:
            missing = [
                item.secret_ref for item in manifest.environment_variables if item.secret_ref
            ]
            if missing:
                raise ExecutableUnitExecutionError(
                    f"secret resolver required for refs: {', '.join(sorted(set(missing)))}"
                )
            environment.update(
                {
                    item.name: item.value
                    for item in manifest.environment_variables
                    if item.value is not None
                }
            )
            return environment, secret_env_keys
        secret_env_keys = {
            item.name for item in manifest.environment_variables if item.secret_ref is not None
        }
        environment.update(resolver.resolve_environment_variables(manifest.environment_variables))
        return environment, secret_env_keys

    def _effective_timeout(
        self,
        configured_timeout: float | None,
        policy_timeout: float | None,
    ) -> float | None:
        """Choose the tighter timeout when both manifest and policy specify one."""
        if configured_timeout is None:
            return policy_timeout
        if policy_timeout is None:
            return configured_timeout
        return min(configured_timeout, policy_timeout)

    def _resource_constraints_for(
        self,
        manifest: WrappedCommandUnitManifest | ProjectUnitManifest,
        enforcement_context: Mapping[str, Any],
    ) -> ResourceConstraints | None:
        """Translate manifest limits plus policy network mode into sandbox constraints."""
        network_mode = enforcement_context.get("network_mode")
        network_access = manifest.resource_limits.network_access
        if network_mode is not None:
            network_access = str(network_mode).lower() not in {"disabled", "deny", "none", "off"}
        constraints = ResourceConstraints(
            cpu_cores=manifest.resource_limits.cpu_cores,
            memory_mb=manifest.resource_limits.memory_mb,
            disk_mb=None,
            max_processes=manifest.resource_limits.max_processes,
            network_access=network_access,
        )
        return constraints if constraints.requires_hard_isolation() else None

    def _sandbox_strictness_mode_for(
        self,
        enforcement_context: Mapping[str, Any],
    ) -> SandboxStrictnessMode | None:
        """Parse the policy sandbox strictness override if one was provided."""
        raw = enforcement_context.get("sandbox_strictness_mode")
        if raw is None:
            return None
        return SandboxStrictnessMode(str(raw))

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
    "ExecutableUnitAdmissionError",
    "ExecutableUnitBinding",
    "ExecutableUnitExecutionError",
    "ExecutableUnitInputError",
    "ExecutableUnitNotFoundError",
    "ExecutableUnitRegistry",
    "ExecutableUnitRunResult",
    "ExecutableUnitRunner",
]
