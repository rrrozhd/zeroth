"""Sandbox helpers for running executable units in isolated environments.

Provides tools for building restricted environments (only allowing certain
env vars), running commands in temporary directories, optionally running
inside Docker containers, and caching environment setups for performance.

The implementation is intentionally self-contained so later orchestration code
can reuse the same environment-building and cache-key logic without pulling in
additional dependencies.
"""

from __future__ import annotations

import inspect
import ipaddress
import json
import os
import re
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from functools import partial
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from zeroth.integrations.execution.constraints import (
    ResourceConstraints,
    build_docker_resource_flags,
)


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
    source = dict(os.environ if base_env is None else base_env)
    environment: dict[str, str] = {}
    allowed = set() if allowed_env_keys is None else {key for key in allowed_env_keys}
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
    SIDECAR = "sidecar"


@dataclass(frozen=True, slots=True)
class DockerSandboxConfig:
    """Settings for the Docker container used as a sandbox.

    Includes the container name, the Docker binary path, and where files
    go inside the container.
    """

    container_name: str = "zeroth-sandbox"
    docker_binary: str = "docker"
    workspace_root: str = "/tmp/zeroth-sandbox"
    # Container-hardening defaults: immutable root filesystem (the workspace
    # bind mount and a /tmp tmpfs stay writable), all capabilities dropped,
    # and privilege escalation blocked. Disable only for images that cannot
    # run under these restrictions.
    hardened: bool = True
    # Optional uid[:gid] to run the container as (e.g. "65534:65534").
    # Off by default: the bind-mounted workspace is host-owned, so forcing a
    # non-root user breaks images whose units write outputs there.
    run_as_user: str | None = None
    max_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        if self.max_output_bytes < 0:
            raise ValueError("max_output_bytes must be non-negative")


class SandboxStrictnessMode(StrEnum):
    """How strongly the sandbox should insist on hardened isolation."""

    PERMISSIVE = "permissive"
    STANDARD = "standard"
    STRICT = "strict"


def build_docker_hardening_flags(
    *, hardened: bool = True, run_as_user: str | None = None
) -> list[str]:
    """Build the shared fail-closed hardening flags for untrusted containers."""
    if not hardened:
        flags: list[str] = []
    else:
        flags = [
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp",
        ]
    if run_as_user:
        flags.extend(["--user", run_as_user])
    return flags


def _docker_hardening_flags(docker: DockerSandboxConfig) -> list[str]:
    """Container-hardening flags applied to every sandbox `docker run`."""
    return build_docker_hardening_flags(
        hardened=docker.hardened,
        run_as_user=docker.run_as_user,
    )


_IMAGE_COMPONENT = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*\Z")
_IMAGE_HOST_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_IMAGE_REGISTRY_HOST = re.compile(rf"(?:localhost|{_IMAGE_HOST_LABEL}(?:\.{_IMAGE_HOST_LABEL})*)\Z")
_IMAGE_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_IMAGE_DIGEST_LENGTHS = {"sha256": 64, "sha384": 96, "sha512": 128}


def _reject_docker_image() -> None:
    raise SandboxPolicyViolationError("sandbox image reference violates execution policy")


def _validate_registry_component(registry: str) -> None:
    if registry.startswith("["):
        closing_bracket = registry.find("]")
        if closing_bracket <= 1:
            _reject_docker_image()
        address = registry[1:closing_bracket]
        if "%" in address:
            _reject_docker_image()
        try:
            ipaddress.IPv6Address(address)
        except ipaddress.AddressValueError:
            _reject_docker_image()
        suffix = registry[closing_bracket + 1 :]
        if suffix and not suffix.startswith(":"):
            _reject_docker_image()
        port = suffix[1:] if suffix else ""
        if suffix and not port:
            _reject_docker_image()
    else:
        if "[" in registry or "]" in registry or registry.count(":") > 1:
            _reject_docker_image()
        host, separator, port = registry.rpartition(":")
        if not separator:
            host, port = registry, ""
        elif not port:
            _reject_docker_image()
        if not _IMAGE_REGISTRY_HOST.fullmatch(host):
            _reject_docker_image()
    if port and (not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65_535):
        _reject_docker_image()


def _validate_image_digest(digest: str) -> None:
    algorithm, separator, encoded = digest.partition(":")
    expected_length = _IMAGE_DIGEST_LENGTHS.get(algorithm)
    if (
        not separator
        or expected_length is None
        or len(encoded) != expected_length
        or not re.fullmatch(r"[0-9a-f]+", encoded)
    ):
        _reject_docker_image()


def validate_docker_image_reference(image: str) -> str:
    """Reject option-like or malformed image references before Docker argv assembly."""
    if (
        not image
        or image.startswith("-")
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in image
        )
        or image.count("@") > 1
    ):
        _reject_docker_image()

    name_and_tag, separator, digest = image.partition("@")
    if separator:
        _validate_image_digest(digest)

    last_component = name_and_tag.rsplit("/", 1)[-1]
    if ":" in last_component:
        repository, tag = name_and_tag.rsplit(":", 1)
        if not _IMAGE_TAG.fullmatch(tag):
            _reject_docker_image()
    else:
        repository = name_and_tag

    if not repository or len(repository.encode()) > 255:
        _reject_docker_image()

    components = repository.split("/")
    if any(not component for component in components):
        _reject_docker_image()
    if len(components) > 1 and (
        "." in components[0]
        or ":" in components[0]
        or components[0] == "localhost"
        or components[0].startswith("[")
    ):
        registry, *components = components
        _validate_registry_component(registry)
    if not components or any(not _IMAGE_COMPONENT.fullmatch(item) for item in components):
        _reject_docker_image()
    return image


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Top-level config that picks which backend to use and Docker settings."""

    backend: SandboxBackendMode = SandboxBackendMode.LOCAL
    docker: DockerSandboxConfig = field(default_factory=DockerSandboxConfig)
    strictness_mode: SandboxStrictnessMode = SandboxStrictnessMode.STANDARD
    # Explicit escape hatch for local development fixtures only. Production
    # settings validation rejects it, and untrusted manifests otherwise cannot
    # reach the host subprocess backend even under permissive strictness.
    allow_untrusted_local_development: bool = False
    sidecar_url: str | None = None  # e.g., "http://sandbox-sidecar:8001"


_sandbox_config_parameters = inspect.signature(SandboxConfig).parameters
SandboxConfig.__signature__ = inspect.signature(SandboxConfig).replace(
    parameters=[
        parameter
        for name, parameter in _sandbox_config_parameters.items()
        if name != "allow_untrusted_local_development"
    ]
)


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
    stdout_truncated: bool = False
    stderr_truncated: bool = False


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


class _SandboxExecutionCancelledError(Exception):
    """Internal signal: the owning async task requested execution cleanup."""


class SandboxBackendUnavailableError(RuntimeError):
    """Raised when the requested backend (e.g., Docker) is not running or accessible."""


class SandboxPolicyViolationError(SandboxBackendUnavailableError):
    """Raised when a required isolation or enforcement level cannot be satisfied."""


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
        process_factory: Callable[..., subprocess.Popen[bytes]] | None = None,
        container_inspector: Callable[[str], bool] | None = None,
        sidecar_client: Any | None = None,
    ) -> None:
        self._base_env = dict(os.environ if base_env is None else base_env)
        self._cache_manager = cache_manager or EnvironmentCacheManager()
        self._config = config or SandboxConfig()
        self._command_runner = command_runner or subprocess.run
        self._process_factory = process_factory or subprocess.Popen
        self._container_inspector = container_inspector
        self._sidecar_client = sidecar_client

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
        resource_constraints: ResourceConstraints | None = None,
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
            try:
                host_cwd.resolve(strict=False).relative_to(sandbox_root.resolve())
            except ValueError as exc:
                raise ValueError("working_directory must be contained by the sandbox root") from exc
            host_cwd.mkdir(parents=True, exist_ok=True)
            backend = self._resolve_backend(resource_constraints)
            if backend is SandboxBackendMode.SIDECAR:
                return self._run_via_sidecar(
                    command=command,
                    input_text=input_text,
                    timeout_seconds=timeout_seconds,
                    environment=env_snapshot,
                    resource_constraints=resource_constraints,
                    sandbox_root=sandbox_root,
                    relative_cwd=relative_cwd,
                )
            if backend is SandboxBackendMode.DOCKER:
                return self._run_in_docker(
                    command=command,
                    input_text=input_text,
                    timeout_seconds=timeout_seconds,
                    sandbox_root=sandbox_root,
                    relative_cwd=relative_cwd,
                    environment=env_snapshot,
                    resource_constraints=resource_constraints,
                )
            return self._run_locally(
                command=command,
                input_text=input_text,
                timeout_seconds=timeout_seconds,
                cwd=host_cwd,
                environment=env_snapshot,
                resource_constraints=resource_constraints,
            )

    def _resolve_backend(
        self,
        resource_constraints: ResourceConstraints | None = None,
    ) -> SandboxBackendMode:
        """Decide which backend to use based on config and Docker availability."""
        configured = self._config.backend
        strictness = self._config.strictness_mode
        if (
            strictness is SandboxStrictnessMode.STRICT
            and configured is not SandboxBackendMode.LOCAL
            and not self._config.docker.hardened
        ):
            raise SandboxPolicyViolationError("strict sandbox mode requires container hardening")
        if configured is SandboxBackendMode.SIDECAR:
            if self._sidecar_client is None:
                raise SandboxBackendUnavailableError("sidecar client not configured")
            return SandboxBackendMode.SIDECAR
        if configured is SandboxBackendMode.LOCAL:
            # STRICT refuses LOCAL unconditionally (audit S2): a local subprocess
            # can never provide hardened isolation, so gating the refusal on
            # resource_constraints made STRICT a silent no-op for a bare inline
            # unit (constraints=None) — the strict knob's whole purpose is to be
            # the guardrail when the backend isn't hardened.
            if strictness is SandboxStrictnessMode.STRICT:
                raise SandboxPolicyViolationError(
                    "strict sandbox strictness cannot run on the local backend "
                    "(no hardened isolation)"
                )
            if (
                strictness is not SandboxStrictnessMode.PERMISSIVE  # STANDARD
                and resource_constraints is not None
                and resource_constraints.requires_hard_isolation()
            ):
                raise SandboxPolicyViolationError(
                    "local sandbox backend cannot satisfy the requested isolation constraints"
                )
            return SandboxBackendMode.LOCAL
        docker = self._config.docker
        available = self._docker_container_available(docker.container_name)
        if configured is SandboxBackendMode.AUTO:
            if available:
                return SandboxBackendMode.DOCKER
            if strictness is SandboxStrictnessMode.PERMISSIVE:
                return SandboxBackendMode.LOCAL
            raise SandboxPolicyViolationError(
                f"docker sandbox container {docker.container_name!r} is not available"
            )
        if not available:
            error_type = (
                SandboxPolicyViolationError
                if strictness is not SandboxStrictnessMode.PERMISSIVE
                else SandboxBackendUnavailableError
            )
            raise error_type(f"docker sandbox container {docker.container_name!r} is not available")
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
        if ".." in relative_cwd.parts:
            raise ValueError("working_directory must be contained by the sandbox root")
        return relative_cwd

    def _run_locally(
        self,
        *,
        command: Sequence[str],
        input_text: str | None,
        timeout_seconds: float | None,
        cwd: Path,
        environment: SandboxEnvironment,
        resource_constraints: ResourceConstraints | None = None,
        read_only_paths: Sequence[str] = (),
        cancellation_event: threading.Event | None = None,
    ) -> SandboxExecutionResult:
        """Run a command as a local subprocess with the prepared environment."""
        self._warn_about_unenforced_local_constraints(
            resource_constraints,
            read_only_paths=read_only_paths,
        )
        started_at = time.perf_counter()
        try:
            runner = self._command_runner
            if cancellation_event is not None:
                runner = partial(
                    self._run_cancellable_local_process, cancellation_event=cancellation_event
                )
            started = runner(
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

    def _run_cancellable_local_process(
        self,
        command: list[str],
        *,
        input: str | None,
        timeout: float | None,
        cwd: str,
        env: Mapping[str, str],
        cancellation_event: threading.Event,
        **_options: Any,
    ) -> subprocess.CompletedProcess[str]:
        """Own and reap a local process when its async caller can cancel it."""
        if cancellation_event.is_set():
            raise _SandboxExecutionCancelledError()
        deadline = None if timeout is None else time.perf_counter() + timeout
        process = self._process_factory(
            command,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=os.name == "posix",
        )
        pending_input = input
        try:
            while True:
                if cancellation_event.is_set():
                    raise _SandboxExecutionCancelledError()
                remaining = None if deadline is None else deadline - time.perf_counter()
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(
                        input=pending_input,
                        timeout=0.1 if remaining is None else min(0.1, remaining),
                    )
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
        except BaseException as exc:
            if os.name == "posix":
                # Stop ordinary descendants along with the command, before
                # joining pipes that those descendants may still hold open.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            stdout, stderr = process.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                exc.stdout, exc.stderr = stdout, stderr
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    def _run_in_docker(
        self,
        *,
        command: Sequence[str],
        input_text: str | None,
        timeout_seconds: float | None,
        sandbox_root: Path,
        relative_cwd: Path | None,
        environment: SandboxEnvironment,
        resource_constraints: ResourceConstraints | None = None,
        read_only_paths: Sequence[str] = (),
        cancellation_event: threading.Event | None = None,
    ) -> SandboxExecutionResult:
        """Run a command inside a Docker container.

        Starts an ephemeral container from the provisioned sandbox image,
        bind-mounts the sandbox root, and applies per-run resource flags.
        ``read_only_paths`` names sandbox-relative subtrees remounted read-only
        on top of the read-write root bind mount; entries are validated
        lexically (mirroring the sidecar's workspace-path rule) before any
        Docker invocation. Container creation completes before execution starts,
        so timeout cleanup can address only the workload owned by this call.
        """
        docker = self._config.docker
        container_name = docker.container_name
        container_root = PurePosixPath(docker.workspace_root) / sandbox_root.name
        container_cwd = container_root
        if relative_cwd is not None:
            container_cwd = container_root.joinpath(*relative_cwd.parts)
        # Validated before the image lookup so a hostile entry never buys any
        # docker invocation at all.
        read_only_mount_flags: list[str] = []
        for subtree in read_only_paths:
            validated = _validate_read_only_subtree(subtree)
            read_only_mount_flags.extend(
                ["-v", f"{sandbox_root}/{validated}:{container_root}/{validated}:ro"]
            )
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
        image_ref = self._docker_image_for(container_name)
        validate_docker_image_reference(image_ref)
        constraints = resource_constraints or ResourceConstraints()
        if constraints.network_access is None:
            constraints = ResourceConstraints(
                cpu_cores=constraints.cpu_cores,
                memory_mb=constraints.memory_mb,
                disk_mb=constraints.disk_mb,
                max_processes=constraints.max_processes,
                network_access=False,
            )

        started_at = time.perf_counter()
        workload_name = f"zeroth-sandbox-run-{uuid4().hex}"
        docker_command = [
            docker.docker_binary,
            "create",
            "--name",
            workload_name,
            *(["--interactive"] if input_text is not None else []),
            *_docker_hardening_flags(docker),
            "-v",
            f"{sandbox_root}:{container_root}",
            *read_only_mount_flags,
            *build_docker_resource_flags(constraints),
            *self._docker_env_flags(translated_env),
            "-w",
            str(container_cwd),
            image_ref,
            *translated_command,
        ]
        created = False
        try:
            if cancellation_event is not None and cancellation_event.is_set():
                raise _SandboxExecutionCancelledError()
            creation = self._command_runner(
                docker_command,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
            if creation.returncode != 0:
                raise SandboxBackendUnavailableError("docker sandbox creation failed")
            created = True
            if cancellation_event is not None and cancellation_event.is_set():
                raise _SandboxExecutionCancelledError()
            remaining = (
                None
                if timeout_seconds is None
                else timeout_seconds - (time.perf_counter() - started_at)
            )
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(docker_command, timeout_seconds)
            process = self._process_factory(
                [
                    docker.docker_binary,
                    "start",
                    "--attach",
                    *(["--interactive"] if input_text is not None else []),
                    workload_name,
                ],
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes, stdout_truncated, stderr_truncated = (
                self._communicate_bounded_process(
                    process,
                    input_text=input_text,
                    timeout_seconds=remaining,
                    max_output_bytes=docker.max_output_bytes,
                    cancellation_event=cancellation_event,
                )
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxTimeoutError(
                command=command,
                timeout_seconds=timeout_seconds,
                stdout=self._decode_capped_output(exc.stdout or b"", docker.max_output_bytes),
                stderr=self._decode_capped_output(exc.stderr or b"", docker.max_output_bytes),
            ) from exc
        finally:
            if created:
                # The name is generated by the host, never read from the writable
                # workload directory or taken from the provisioned template.
                try:
                    cleanup = self._command_runner(
                        [docker.docker_binary, "rm", "--force", workload_name],
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise SandboxBackendUnavailableError(
                        f"docker sandbox cleanup failed for {workload_name}"
                    ) from exc
                if cleanup.returncode != 0:
                    raise SandboxBackendUnavailableError(
                        f"docker sandbox cleanup failed for {workload_name}"
                    )
        return SandboxExecutionResult(
            command=tuple(translated_command),
            returncode=process.returncode if process.returncode is not None else 1,
            stdout=self._decode_capped_output(stdout_bytes, docker.max_output_bytes),
            stderr=self._decode_capped_output(stderr_bytes, docker.max_output_bytes),
            workdir=str(container_cwd),
            environment=dict(translated_env),
            duration_seconds=time.perf_counter() - started_at,
            cache_key=environment.cache_key,
            backend=SandboxBackendMode.DOCKER.value,
            container_name=container_name,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def _communicate_bounded_process(
        self,
        process: subprocess.Popen[bytes],
        *,
        input_text: str | None,
        timeout_seconds: float | None,
        max_output_bytes: int,
        cancellation_event: threading.Event | None = None,
    ) -> tuple[bytes, bytes, bool, bool]:
        """Drain Docker output concurrently while retaining bounded byte prefixes."""
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_result: list[tuple[bytes, bool]] = []
        stderr_result: list[tuple[bytes, bool]] = []
        stdout_thread = threading.Thread(
            target=lambda: stdout_result.append(
                self._read_bounded_stream(process.stdout, max_output_bytes)
            ),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=lambda: stderr_result.append(
                self._read_bounded_stream(process.stderr, max_output_bytes)
            ),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        stdin_thread: threading.Thread | None = None
        if input_text is not None and process.stdin is not None:

            def pump_stdin() -> None:
                try:
                    process.stdin.write(input_text.encode())
                    process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass
                finally:
                    with suppress(OSError):
                        process.stdin.close()

            stdin_thread = threading.Thread(
                target=pump_stdin,
                name="zeroth-docker-stdin",
                daemon=True,
            )
            stdin_thread.start()
        try:
            if cancellation_event is None:
                process.wait(timeout=timeout_seconds)
            else:
                deadline = (
                    None if timeout_seconds is None else time.perf_counter() + timeout_seconds
                )
                while True:
                    if cancellation_event.is_set():
                        raise _SandboxExecutionCancelledError()
                    remaining = None if deadline is None else deadline - time.perf_counter()
                    if remaining is not None and remaining <= 0:
                        raise subprocess.TimeoutExpired("sandbox workload", timeout_seconds)
                    try:
                        process.wait(timeout=0.1 if remaining is None else min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
        except BaseException as exc:
            # An interrupted wait must reap the attached CLI as well as letting
            # the caller's finally block remove the daemon-owned workload.
            process.kill()
            if process.stdin is not None:
                with suppress(OSError):
                    process.stdin.close()
            process.wait()
            if stdin_thread is not None:
                stdin_thread.join()
            stdout_thread.join()
            stderr_thread.join()
            if isinstance(exc, subprocess.TimeoutExpired):
                exc.stdout = stdout_result[0][0] if stdout_result else b""
                exc.stderr = stderr_result[0][0] if stderr_result else b""
            raise
        if stdin_thread is not None:
            stdin_thread.join()
        stdout_thread.join()
        stderr_thread.join()
        stdout, stdout_truncated = stdout_result[0]
        stderr, stderr_truncated = stderr_result[0]
        return stdout, stderr, stdout_truncated, stderr_truncated

    @staticmethod
    def _read_bounded_stream(stream: Any, max_output_bytes: int) -> tuple[bytes, bool]:
        retained = bytearray()
        truncated = False
        while chunk := stream.read(65_536):
            raw = chunk.encode() if isinstance(chunk, str) else chunk
            remaining = max_output_bytes - len(retained)
            if remaining > 0:
                retained.extend(raw[:remaining])
            if len(raw) > remaining:
                truncated = True
        return bytes(retained), truncated

    @staticmethod
    def _decode_capped_output(value: bytes | str, max_output_bytes: int) -> str:
        raw = value.encode() if isinstance(value, str) else value
        return raw[:max_output_bytes].decode(errors="ignore")

    def _run_via_sidecar(
        self,
        *,
        command: Sequence[str],
        input_text: str | None,
        timeout_seconds: float | None,
        environment: SandboxEnvironment,
        resource_constraints: ResourceConstraints | None = None,
        sandbox_root: Path | None = None,
        relative_cwd: str | Path | PurePosixPath | None = None,
        read_only_paths: Sequence[str] = (),
        capture_output_file: str | None = None,
        cancellation_event: threading.Event | None = None,
    ) -> SandboxExecutionResult:
        """Dispatch execution to the sandbox sidecar over HTTP.

        Uses a single ``asyncio.run()`` to bridge from the sync ``run()``
        method to the async sidecar client; that is legal only because the
        sole caller reaches this method via ``asyncio.to_thread`` (see the
        runner's ``_run_with_prepared_environment``), so it runs on a
        loop-less worker thread.

        When ``sandbox_root`` is provided the staged tree travels with the
        request: the tree is packed into an uncompressed tar and uploaded
        under a fresh workspace id -- the upload must succeed before
        ``/execute`` is attempted (fail-closed ordering) -- the working
        directory and every host-path env value and command token are
        rewritten to their ``/workspace`` container form, and a captured
        output file returned on the response is written back under
        ``sandbox_root`` (confined first; never fabricated when the sidecar
        reports the payload truncated). Without ``sandbox_root`` the request
        keeps its exact pre-staging shape.
        """
        import asyncio
        import base64
        import uuid

        from zeroth.integrations.execution.io import OutputExtractionError
        from zeroth.integrations.sandbox.models import (
            WORKSPACE_MOUNT_ROOT,
            SidecarExecuteRequest,
        )
        from zeroth.platform.primitives import confine_path

        execution_id = str(uuid.uuid4())
        image_ref = self._config.docker.container_name

        workspace_id: str | None = None
        working_directory = WORKSPACE_MOUNT_ROOT
        translated_env: Mapping[str, str] = environment.variables
        translated_command = [str(item) for item in command]
        host_capture_path: Path | None = None
        workspace_tar: bytes | None = None
        if sandbox_root is not None:
            workspace_id = uuid.uuid4().hex
            container_root = PurePosixPath(WORKSPACE_MOUNT_ROOT)
            relative = (
                PurePosixPath(str(relative_cwd)) if relative_cwd is not None else PurePosixPath()
            )
            working_directory = str(container_root.joinpath(*relative.parts))
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
            if capture_output_file is not None:
                # Confined BEFORE anything leaves this process: a traversal
                # capture path must never buy an upload, an execution, or a
                # write outside the sandbox root.
                host_capture_path = confine_path(
                    capture_output_file,
                    root=sandbox_root,
                    context="sidecar capture output file",
                )
            workspace_tar = _pack_workspace_tar(sandbox_root)

        request = SidecarExecuteRequest(
            execution_id=execution_id,
            image=image_ref,
            command=translated_command,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            environment=dict(translated_env),
            working_directory=working_directory,
            cpu_cores=(resource_constraints.cpu_cores if resource_constraints else None),
            memory_mb=(resource_constraints.memory_mb if resource_constraints else None),
            max_processes=(resource_constraints.max_processes if resource_constraints else None),
            network_access=(resource_constraints.network_access if resource_constraints else False)
            or False,
            workspace_id=workspace_id,
            capture_output_file=capture_output_file if workspace_id is not None else None,
            read_only_paths=list(read_only_paths) if workspace_id is not None else [],
        )

        client = self._sidecar_client

        async def _dispatch() -> Any:
            # Fail-closed ordering: the upload must complete before /execute
            # is attempted, so an upload failure raises here and no execution
            # request is ever sent.
            if cancellation_event is not None and cancellation_event.is_set():
                raise _SandboxExecutionCancelledError()
            if workspace_tar is not None:
                await client.upload_workspace(workspace_id, workspace_tar)
            if cancellation_event is None:
                return await client.execute(request)
            if cancellation_event.is_set():
                raise _SandboxExecutionCancelledError()
            import httpx

            execution = asyncio.create_task(client.execute(request))
            while not execution.done():
                await asyncio.wait({execution}, timeout=0.1)
                if not cancellation_event.is_set() or execution.done():
                    continue
                try:
                    await asyncio.wait_for(client.cancel(execution_id), timeout=10)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        # /execute may still be in flight before registration.
                        # Retain the original request and retry the same id.
                        continue
                    try:
                        await execution
                    finally:
                        raise SandboxBackendUnavailableError(
                            f"sidecar cancellation failed for {execution_id}"
                        ) from exc
                except Exception as exc:
                    # Never abandon the outstanding request merely because the
                    # separate cancel request failed. Its deadline remains in force.
                    try:
                        await execution
                    finally:
                        raise SandboxBackendUnavailableError(
                            f"sidecar cancellation failed for {execution_id}"
                        ) from exc
                await execution
                raise _SandboxExecutionCancelledError()
            response = execution.result()
            if cancellation_event.is_set():
                raise _SandboxExecutionCancelledError()
            return response

        response = asyncio.run(_dispatch())

        if sandbox_root is not None and response.output_file_truncated:
            # Never fabricate a partial output file: surface the same bounded,
            # truthful error family ``extract_output`` uses for an oversized
            # ``zeroth-output.json``.
            raise OutputExtractionError(
                "output file exceeds the sidecar capture byte cap; "
                "the truncated payload was withheld"
            )
        if host_capture_path is not None and response.output_file_b64 is not None:
            host_capture_path.parent.mkdir(parents=True, exist_ok=True)
            host_capture_path.write_bytes(base64.b64decode(response.output_file_b64))

        return SandboxExecutionResult(
            command=tuple(translated_command),
            returncode=response.returncode if response.returncode is not None else 1,
            stdout=response.stdout,
            stderr=response.stderr,
            workdir=request.working_directory,
            environment=dict(translated_env),
            timed_out=response.timed_out,
            duration_seconds=response.duration_seconds,
            cache_key=environment.cache_key,
            backend=SandboxBackendMode.SIDECAR.value,
            stdout_truncated=response.stdout_truncated,
            stderr_truncated=response.stderr_truncated,
        )

    def _docker_image_for(self, container_name: str) -> str:
        """Resolve the image reference used by the provisioned sandbox container."""
        docker_binary = self._config.docker.docker_binary
        result = self._command_runner(
            [docker_binary, "inspect", "-f", "{{.Config.Image}}", container_name],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise SandboxBackendUnavailableError(
                "docker sandbox container "
                f"{container_name!r} image lookup failed: {result.stderr or result.stdout}"
            )
        return result.stdout.strip()

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

    def _warn_about_unenforced_local_constraints(
        self,
        resource_constraints: ResourceConstraints | None,
        read_only_paths: Sequence[str] = (),
    ) -> None:
        """Warn when local subprocess execution cannot fully honor requested limits."""
        if read_only_paths:
            warnings.warn(
                "local sandbox backend does not enforce read-only path constraints",
                stacklevel=2,
            )
        if resource_constraints is None:
            return
        if resource_constraints.cpu_cores is not None:
            warnings.warn("local sandbox backend does not enforce CPU constraints", stacklevel=2)
        if resource_constraints.memory_mb is not None:
            warnings.warn("local sandbox backend does not enforce memory constraints", stacklevel=2)
        if resource_constraints.disk_mb is not None:
            warnings.warn("local sandbox backend does not enforce disk constraints", stacklevel=2)
        if resource_constraints.max_processes is not None:
            warnings.warn(
                "local sandbox backend does not enforce process-count constraints",
                stacklevel=2,
            )
        if resource_constraints.network_access is not None:
            warnings.warn(
                "local sandbox backend does not enforce network-access constraints",
                stacklevel=2,
            )


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


def _validate_read_only_subtree(value: str) -> str:
    """Lexically validate one read-only subtree entry for a ``-v ...:ro`` mount.

    Reuses the sidecar's workspace-relative-path validator so both backends
    accept exactly the same paths (relative POSIX, no ``..``/empty/``.`` part,
    no NUL, no backslash, no colon -- a colon would split the ``-v`` docker
    token the path is embedded in), re-raised in the sandbox error family. The
    rejected value is caller-controlled and is never echoed.
    """
    from zeroth.integrations.sandbox.models import validate_workspace_relative_path

    try:
        return validate_workspace_relative_path(value)
    except ValueError as exc:
        raise SandboxPolicyViolationError(
            "read-only sandbox paths must be clean relative POSIX paths"
        ) from exc


def _add_workspace_member(
    archive: tarfile.TarFile,
    entry: Path,
    sandbox_root: Path,
    *,
    is_dir: bool,
) -> None:
    """Author one tar header for a staged tree entry, refusing anything special."""
    if entry.is_symlink():
        raise SandboxPolicyViolationError(
            "sandbox workspace contains a non-regular member; refusing to stage it"
        )
    info = tarfile.TarInfo(entry.relative_to(sandbox_root).as_posix())
    if is_dir:
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        archive.addfile(info)
        return
    stats = entry.lstat()
    if not stat.S_ISREG(stats.st_mode):
        raise SandboxPolicyViolationError(
            "sandbox workspace contains a non-regular member; refusing to stage it"
        )
    info.type = tarfile.REGTYPE
    info.size = stats.st_size
    info.mode = 0o755 if stats.st_mode & 0o111 else 0o644
    with entry.open("rb") as handle:
        archive.addfile(info, handle)


def _pack_workspace_tar(sandbox_root: Path) -> bytes:
    """Pack a staged sandbox tree into an uncompressed POSIX tar stream.

    Belt for a tree the runner itself staged: only regular files and
    directories are packed -- a symlink or any other special file is refused
    outright rather than resolved or skipped. Member order is deterministic
    (sorted walk, directories before files at each level, parents before
    children) and every header is authored here (uid/gid 0, mtime 0, mode
    clamped to ``0o755``/``0o644`` preserving the exec bit), so identical
    trees produce identical archives. The sidecar re-validates and re-authors
    the archive server-side; this packer simply never hands it anything but
    the plain tree.
    """
    buffer = BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for current, dirnames, filenames in os.walk(sandbox_root, followlinks=False):
            dirnames.sort()
            filenames.sort()
            current_path = Path(current)
            for name in dirnames:
                _add_workspace_member(archive, current_path / name, sandbox_root, is_dir=True)
            for name in filenames:
                _add_workspace_member(archive, current_path / name, sandbox_root, is_dir=False)
    return buffer.getvalue()


__all__ = [
    "DockerSandboxConfig",
    "EnvironmentCacheManager",
    "SandboxBackendMode",
    "SandboxBackendUnavailableError",
    "SandboxConfig",
    "SandboxEnvironment",
    "SandboxExecutionResult",
    "SandboxManager",
    "SandboxPolicyViolationError",
    "SandboxStrictnessMode",
    "SandboxTimeoutError",
    "build_docker_hardening_flags",
    "build_sandbox_environment",
    "compute_environment_cache_key",
    "docker_container_running",
    "validate_docker_image_reference",
]
