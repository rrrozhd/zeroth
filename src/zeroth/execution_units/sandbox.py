"""Sandbox helpers for running executable units in isolated environments.

Provides tools for building restricted environments (only allowing certain
env vars), running commands in temporary directories, optionally running
inside Docker containers, and caching environment setups for performance.

The implementation is intentionally self-contained so later orchestration code
can reuse the same environment-building and cache-key logic without pulling in
additional dependencies.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any


def _normalize(value: Any) -> Any:
    """Recursively sort and normalize a value so it produces consistent JSON."""
    if isinstance(value, Mapping):
        return {
            str(key): _normalize(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, set):
        return [_normalize(item) for item in sorted(value, key=repr)]
    if isinstance(value, Path):
        return str(value)
    return value


def _canonical_json(value: Any) -> str:
    """Produce a deterministic JSON string for hashing purposes."""
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def compute_environment_cache_key(
    *,
    runtime: str,
    runtime_version: str | None = None,
    dependency_manifest: Mapping[str, Any] | Sequence[Any] | None = None,
    build_config: Mapping[str, Any] | Sequence[Any] | None = None,
    sandbox_policy: Mapping[str, Any] | Sequence[Any] | None = None,
    identity: Mapping[str, Any] | Sequence[Any] | None = None,
) -> str:
    """Create a unique hash key for an execution environment setup.

    Two environments with the same runtime, dependencies, build config, and
    policy will always produce the same key. This lets us cache and reuse
    environments instead of rebuilding them every time.
    """

    payload = {
        "runtime": runtime,
        "runtime_version": runtime_version,
        "dependency_manifest": dependency_manifest,
        "build_config": build_config,
        "sandbox_policy": sandbox_policy,
        "identity": identity,
    }
    digest = sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sandbox-env:{digest}"


def build_sandbox_environment(
    base_env: Mapping[str, str] | None,
    *,
    allowed_env_keys: Sequence[str] | None = None,
    overlay: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a restricted set of environment variables for sandbox execution.

    Starts from the base environment, keeps only the allowed keys, then
    adds any overlay variables on top. This prevents leaking sensitive
    environment variables into sandboxed processes.
    """

    source = dict(base_env or os.environ)
    environment: dict[str, str] = {}
    allowed = set(source) if allowed_env_keys is None else {key for key in allowed_env_keys}
    for key in allowed:
        if key in source:
            environment[key] = str(source[key])
    for key, value in (overlay or {}).items():
        environment[str(key)] = str(value)
    return environment


class SandboxBackendMode(StrEnum):
    """Where sandboxed commands actually run.

    LOCAL runs directly on the host machine, DOCKER runs inside a container,
    and AUTO picks Docker if available, falling back to local.
    """

    LOCAL = "local"
    DOCKER = "docker"
    AUTO = "auto"


@dataclass(frozen=True, slots=True)
class DockerSandboxConfig:
    """Settings for the Docker container used as a sandbox.

    Includes the container name, the Docker binary path, and where files
    go inside the container.
    """

    container_name: str = "zeroth-sandbox"
    docker_binary: str = "docker"
    workspace_root: str = "/tmp/zeroth-sandbox"


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Top-level config that picks which backend to use and Docker settings."""

    backend: SandboxBackendMode = SandboxBackendMode.LOCAL
    docker: DockerSandboxConfig = field(default_factory=DockerSandboxConfig)


@dataclass(frozen=True, slots=True)
class SandboxEnvironment:
    """A snapshot of a prepared execution environment.

    Contains the cache key (for looking it up later), the environment
    variables to use, and any extra metadata.
    """

    cache_key: str
    variables: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SandboxExecutionResult:
    """Everything that came back from running a command in the sandbox.

    Includes the command that was run, its exit code, stdout/stderr output,
    how long it took, and which backend was used.
    """

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    workdir: str
    environment: dict[str, str]
    timed_out: bool = False
    duration_seconds: float | None = None
    cache_key: str | None = None
    backend: str = SandboxBackendMode.LOCAL.value
    container_name: str | None = None


class SandboxTimeoutError(TimeoutError):
    """Raised when a sandboxed process takes longer than its allowed timeout."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        timeout_seconds: float | None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.command = tuple(command)
        self.timeout_seconds = timeout_seconds
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(str(self))

    def __str__(self) -> str:
        """Format a readable error message showing the command and timeout."""
        timeout = "unbounded" if self.timeout_seconds is None else f"{self.timeout_seconds}s"
        return f"sandbox command {' '.join(self.command)} timed out after {timeout}"


class SandboxBackendUnavailableError(RuntimeError):
    """Raised when the requested backend (e.g., Docker) is not running or accessible."""


class EnvironmentCacheManager:
    """Stores prepared sandbox environments in memory so they can be reused.

    This avoids rebuilding the same environment setup every time a unit runs
    with the same configuration.
    """

    def __init__(self) -> None:
        self._cache: dict[str, SandboxEnvironment] = {}

    def get(self, cache_key: str) -> SandboxEnvironment | None:
        """Look up a cached environment by its key. Returns None if not found."""
        return self._cache.get(cache_key)

    def put(
        self,
        cache_key: str,
        environment: Mapping[str, str],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> SandboxEnvironment:
        """Store an environment in the cache and return it."""
        snapshot = SandboxEnvironment(
            cache_key=cache_key,
            variables=dict(environment),
            metadata=dict(metadata or {}),
        )
        self._cache[cache_key] = snapshot
        return snapshot

    def resolve(
        self,
        cache_key: str,
        builder: Callable[[], Mapping[str, str] | SandboxEnvironment],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> SandboxEnvironment:
        """Get from cache if available, otherwise build, cache, and return it."""
        cached = self.get(cache_key)
        if cached is not None:
            return cached
        built = builder()
        if isinstance(built, SandboxEnvironment):
            snapshot = built
        else:
            snapshot = SandboxEnvironment(
                cache_key=cache_key,
                variables=dict(built),
                metadata=dict(metadata or {}),
            )
        self._cache[cache_key] = snapshot
        return snapshot

    def snapshot(self) -> dict[str, SandboxEnvironment]:
        """Return a copy of all cached environments."""
        return dict(self._cache)


class SandboxManager:
    """Manages running commands in isolated sandbox environments.

    Handles environment preparation, caching, and dispatching to either
    local subprocess execution or Docker container execution. This is the
    main entry point for sandboxed command execution.
    """

    def __init__(
        self,
        *,
        base_env: Mapping[str, str] | None = None,
        cache_manager: EnvironmentCacheManager | None = None,
        config: SandboxConfig | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        container_inspector: Callable[[str], bool] | None = None,
    ) -> None:
        self._base_env = dict(base_env or os.environ)
        self._cache_manager = cache_manager or EnvironmentCacheManager()
        self._config = config or SandboxConfig()
        self._command_runner = command_runner or subprocess.run
        self._container_inspector = container_inspector

    @property
    def cache_manager(self) -> EnvironmentCacheManager:
        """Access the environment cache manager for this sandbox."""
        return self._cache_manager

    def prepare_environment(
        self,
        *,
        allowed_env_keys: Sequence[str] | None = None,
        overlay: Mapping[str, str] | None = None,
        cache_identity: Mapping[str, Any] | Sequence[Any] | None = None,
        runtime: str = "local-subprocess",
        runtime_version: str | None = None,
        dependency_manifest: Mapping[str, Any] | Sequence[Any] | None = None,
        build_config: Mapping[str, Any] | Sequence[Any] | None = None,
        sandbox_policy: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> SandboxEnvironment:
        """Build (or retrieve from cache) a sandbox environment for a unit.

        Computes a cache key from the runtime and dependency info, then either
        returns a cached environment or builds a new one.
        """
        cache_key = compute_environment_cache_key(
            runtime=runtime,
            runtime_version=runtime_version,
            dependency_manifest=dependency_manifest,
            build_config=build_config,
            sandbox_policy=sandbox_policy,
            identity={
                "allowed_env_keys": list(allowed_env_keys or []),
                "overlay": dict(overlay or {}),
                "cache_identity": cache_identity,
            },
        )
        return self._cache_manager.resolve(
            cache_key,
            lambda: build_sandbox_environment(
                self._base_env,
                allowed_env_keys=allowed_env_keys,
                overlay=overlay,
            ),
            metadata={
                "runtime": runtime,
                "runtime_version": runtime_version,
                "dependency_manifest": _normalize(dependency_manifest),
                "build_config": _normalize(build_config),
                "sandbox_policy": _normalize(sandbox_policy),
                "cache_identity": _normalize(cache_identity),
            },
        )

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
        allowed_env_keys: Sequence[str] | None = None,
        overlay_env: Mapping[str, str] | None = None,
        working_directory: str | Path | None = None,
        runtime_version: str | None = None,
        dependency_manifest: Mapping[str, Any] | Sequence[Any] | None = None,
        build_config: Mapping[str, Any] | Sequence[Any] | None = None,
        sandbox_policy: Mapping[str, Any] | Sequence[Any] | None = None,
        cache_identity: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> SandboxExecutionResult:
        """Run a command in a sandboxed environment.

        Prepares the environment, creates a temp directory, and dispatches
        to either local or Docker execution depending on the config.
        """
        env_snapshot = self.prepare_environment(
            allowed_env_keys=allowed_env_keys,
            overlay=overlay_env,
            cache_identity=cache_identity,
            runtime="local-subprocess",
            runtime_version=runtime_version,
            dependency_manifest=dependency_manifest,
            build_config=build_config,
            sandbox_policy=sandbox_policy,
        )
        with tempfile.TemporaryDirectory(prefix="zeroth-sandbox-") as tempdir:
            sandbox_root = Path(tempdir)
            relative_cwd = self._resolve_relative_workdir(working_directory)
            host_cwd = sandbox_root if relative_cwd is None else sandbox_root / relative_cwd
            host_cwd.mkdir(parents=True, exist_ok=True)
            backend = self._resolve_backend()
            if backend is SandboxBackendMode.DOCKER:
                return self._run_in_docker(
                    command=command,
                    input_text=input_text,
                    timeout_seconds=timeout_seconds,
                    sandbox_root=sandbox_root,
                    relative_cwd=relative_cwd,
                    environment=env_snapshot,
                )
            return self._run_locally(
                command=command,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
                cwd=host_cwd,
                environment=env_snapshot,
            )

    def _resolve_backend(self) -> SandboxBackendMode:
        """Decide which backend to use based on config and Docker availability."""
        configured = self._config.backend
        if configured is SandboxBackendMode.LOCAL:
            return SandboxBackendMode.LOCAL
        docker = self._config.docker
        available = self._docker_container_available(docker.container_name)
        if configured is SandboxBackendMode.AUTO:
            return SandboxBackendMode.DOCKER if available else SandboxBackendMode.LOCAL
        if not available:
            raise SandboxBackendUnavailableError(
                f"docker sandbox container {docker.container_name!r} is not available"
            )
        return SandboxBackendMode.DOCKER

    def _docker_container_available(self, container_name: str) -> bool:
        """Check if a Docker container is running and ready to use."""
        inspector = self._container_inspector
        if inspector is not None:
            return bool(inspector(container_name))
        return docker_container_running(
            container_name,
            docker_binary=self._config.docker.docker_binary,
            command_runner=self._command_runner,
        )

    def _resolve_relative_workdir(self, working_directory: str | Path | None) -> Path | None:
        """Validate and return the working directory as a relative path."""
        if working_directory is None:
            return None
        relative_cwd = Path(working_directory)
        if relative_cwd.is_absolute():
            raise ValueError("working_directory must be relative to the sandbox root")
        return relative_cwd

    def _run_locally(
        self,
        *,
        command: Sequence[str],
        input_text: str | None,
        timeout_seconds: float | None,
        cwd: Path,
        environment: SandboxEnvironment,
    ) -> SandboxExecutionResult:
        """Run a command as a local subprocess with the prepared environment."""
        started_at = time.perf_counter()
        try:
            started = self._command_runner(
                list(command),
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                cwd=str(cwd),
                env=environment.variables,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxTimeoutError(
                command=command,
                timeout_seconds=timeout_seconds,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
            ) from exc
        return SandboxExecutionResult(
            command=tuple(command),
            returncode=started.returncode,
            stdout=started.stdout,
            stderr=started.stderr,
            workdir=str(cwd),
            environment=dict(environment.variables),
            duration_seconds=time.perf_counter() - started_at,
            cache_key=environment.cache_key,
            backend=SandboxBackendMode.LOCAL.value,
        )

    def _run_in_docker(
        self,
        *,
        command: Sequence[str],
        input_text: str | None,
        timeout_seconds: float | None,
        sandbox_root: Path,
        relative_cwd: Path | None,
        environment: SandboxEnvironment,
    ) -> SandboxExecutionResult:
        """Run a command inside a Docker container.

        Copies files into the container, runs the command, copies results
        back out, and cleans up the container workspace.
        """
        docker = self._config.docker
        container_name = docker.container_name
        container_root = PurePosixPath(docker.workspace_root) / sandbox_root.name
        container_cwd = container_root
        if relative_cwd is not None:
            container_cwd = container_root.joinpath(*relative_cwd.parts)
        translated_env = {
            key: _rewrite_sandbox_path(
                value,
                sandbox_root=sandbox_root,
                container_root=container_root,
            )
            for key, value in environment.variables.items()
        }
        translated_command = [
            _rewrite_sandbox_path(
                str(item),
                sandbox_root=sandbox_root,
                container_root=container_root,
            )
            for item in command
        ]

        self._docker_control(
            container_name,
            "exec",
            container_name,
            "mkdir",
            "-p",
            str(container_root),
        )
        if any(sandbox_root.iterdir()):
            self._docker_control(
                container_name,
                "cp",
                f"{sandbox_root}/.",
                f"{container_name}:{container_root}",
            )

        started_at = time.perf_counter()
        try:
            started = self._command_runner(
                [
                    docker.docker_binary,
                    "exec",
                    *self._docker_env_flags(translated_env),
                    "-w",
                    str(container_cwd),
                    container_name,
                    *translated_command,
                ],
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxTimeoutError(
                command=command,
                timeout_seconds=timeout_seconds,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
            ) from exc
        finally:
            self._docker_control(
                container_name,
                "cp",
                f"{container_name}:{container_root}/.",
                str(sandbox_root),
                allow_failure=True,
            )
            self._docker_control(
                container_name,
                "exec",
                container_name,
                "rm",
                "-rf",
                str(container_root),
                allow_failure=True,
            )
        return SandboxExecutionResult(
            command=tuple(translated_command),
            returncode=started.returncode,
            stdout=started.stdout,
            stderr=started.stderr,
            workdir=str(container_cwd),
            environment=dict(translated_env),
            duration_seconds=time.perf_counter() - started_at,
            cache_key=environment.cache_key,
            backend=SandboxBackendMode.DOCKER.value,
            container_name=container_name,
        )

    def _docker_control(
        self,
        container_name: str,
        verb: str,
        *args: str,
        allow_failure: bool = False,
    ) -> None:
        """Run a Docker CLI command (exec, cp, etc.) for sandbox management."""
        docker_binary = self._config.docker.docker_binary
        result = self._command_runner(
            [docker_binary, verb, *args],
            text=True,
            capture_output=True,
            check=False,
        )
        if allow_failure or result.returncode == 0:
            return
        raise SandboxBackendUnavailableError(
            "docker sandbox container "
            f"{container_name!r} command failed: {result.stderr or result.stdout}"
        )

    def _docker_env_flags(self, environment: Mapping[str, str]) -> list[str]:
        """Build the -e KEY=VALUE flags for docker exec."""
        flags: list[str] = []
        for key, value in environment.items():
            flags.extend(["-e", f"{key}={value}"])
        return flags


def docker_container_running(
    container_name: str,
    *,
    docker_binary: str = "docker",
    command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    """Check if a Docker container with the given name is currently running.

    Returns False if Docker is not installed or the container does not exist.
    """
    runner = command_runner or subprocess.run
    try:
        result = runner(
            [docker_binary, "inspect", "-f", "{{.State.Running}}", container_name],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _rewrite_sandbox_path(
    value: str,
    *,
    sandbox_root: Path,
    container_root: PurePosixPath,
) -> str:
    """Replace host sandbox paths with the equivalent container paths."""
    sandbox_prefix = str(sandbox_root)
    if value == sandbox_prefix:
        return str(container_root)
    prefix = f"{sandbox_prefix}{os.sep}"
    if not value.startswith(prefix):
        return value
    suffix = value[len(prefix) :].replace(os.sep, "/")
    return str(container_root / PurePosixPath(suffix))


__all__ = [
    "DockerSandboxConfig",
    "EnvironmentCacheManager",
    "SandboxBackendMode",
    "SandboxBackendUnavailableError",
    "SandboxConfig",
    "SandboxEnvironment",
    "SandboxExecutionResult",
    "SandboxManager",
    "SandboxTimeoutError",
    "build_sandbox_environment",
    "compute_environment_cache_key",
    "docker_container_running",
]
