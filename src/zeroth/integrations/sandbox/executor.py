"""Docker execution logic for the sandbox sidecar.

Runs untrusted code inside Docker containers with per-execution network
isolation. Each execution gets its own ``--internal`` Docker network to
prevent outbound access unless explicitly permitted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from zeroth.integrations.execution.constraints import (
    ResourceConstraints,
    build_docker_resource_flags,
)
from zeroth.integrations.execution.sandbox import (
    SandboxPolicyViolationError,
    build_docker_hardening_flags,
    validate_docker_image_reference,
)
from zeroth.integrations.sandbox.models import (
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarStatusResponse,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ExecutionState:
    """Mutable lifecycle state for one uniquely identified execution."""

    task: asyncio.Task[Any]
    process: asyncio.subprocess.Process | None = None
    cancel_requested: bool = False
    stop_started: bool = False
    owns_network: bool = False
    terminal: bool = False
    cleanup_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cleanup_done: asyncio.Event = field(default_factory=asyncio.Event)


class SidecarExecutor:
    """Executes commands inside isolated Docker containers.

    Each execution creates a dedicated Docker network with ``--internal``
    to block outbound traffic, runs the container with resource limits,
    captures output, and tears down the network on completion.
    """

    def __init__(self, *, docker_binary: str = "docker", max_output_bytes: int = 1_048_576) -> None:
        if max_output_bytes < 0:
            raise ValueError("max_output_bytes must be non-negative")
        self._docker_binary = docker_binary
        self._max_output_bytes = max_output_bytes
        self._executions: dict[str, SidecarExecuteResponse] = {}
        self._states: dict[str, _ExecutionState] = {}
        self._registry_lock = asyncio.Lock()

    async def execute(self, request: SidecarExecuteRequest) -> SidecarExecuteResponse:
        """Run a command in an isolated Docker container."""
        validate_docker_image_reference(request.image)
        network_name = f"zeroth-sandbox-{request.execution_id}"
        started_at = time.perf_counter()
        execution_task = asyncio.current_task()
        assert execution_task is not None
        async with self._registry_lock:
            if request.execution_id in self._states or request.execution_id in self._executions:
                raise SandboxPolicyViolationError("sandbox execution request violates policy")
            state = _ExecutionState(task=execution_task)
            self._states[request.execution_id] = state
        self._executions[request.execution_id] = SidecarExecuteResponse(
            execution_id=request.execution_id,
            status="running",
        )

        # Build resource constraints for the CPU/memory/pids flags ONLY. The
        # network dimension is deliberately left None (audit B11): this executor
        # owns network attachment via the per-execution --internal network below
        # plus the explicit `--network={network_name}` on `docker run`. Passing
        # request.network_access here would make build_docker_resource_flags emit
        # a SECOND `--network` token, and docker aborts with exit 125
        # ("conflicting options: cannot attach both user-defined and
        # non-user-defined network-modes") — every sidecar execution failing.
        constraints = ResourceConstraints(
            cpu_cores=request.cpu_cores,
            memory_mb=request.memory_mb,
            max_processes=request.max_processes,
            network_access=None,
        )

        try:
            # Step 1: Create isolated network
            network_flags = ["--internal"] if not request.network_access else []
            await self._run_cmd(
                self._docker_binary,
                "network",
                "create",
                *network_flags,
                network_name,
            )
            state.owns_network = True
            if state.cancel_requested:
                return self._persist_cancelled(request.execution_id, started_at)

            # Step 2: Build docker run command
            resource_flags = build_docker_resource_flags(constraints)
            env_flags: list[str] = []
            for key, value in request.environment.items():
                env_flags.extend(["-e", f"{key}={value}"])

            cmd = [
                self._docker_binary,
                "run",
                "--rm",
                *build_docker_hardening_flags(),
                f"--network={network_name}",
                *resource_flags,
                *env_flags,
                "-w",
                request.working_directory,
                request.image,
                *request.command,
            ]

            # Step 3: Execute with timeout
            timed_out = False
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE if request.input_text else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                async with state.lock:
                    state.process = proc
                if state.cancel_requested:
                    await self._stop_process(state)
                    return self._persist_cancelled(request.execution_id, started_at)
                stdin_bytes = request.input_text.encode() if request.input_text else None
                (
                    stdout_bytes,
                    stderr_bytes,
                    stdout_truncated,
                    stderr_truncated,
                ) = await asyncio.wait_for(
                    self._communicate_bounded(proc, stdin_bytes),
                    timeout=request.timeout_seconds,
                )
                returncode = proc.returncode
            except TimeoutError:
                timed_out = True
                await self._stop_process(state)
                stdout_bytes = b""
                stderr_bytes = b"Execution timed out"
                stdout_truncated = False
                stderr_truncated = False
                returncode = -1

            async with state.lock:
                duration = time.perf_counter() - started_at
                status = "completed" if returncode == 0 else "failed"
                if timed_out:
                    status = "failed"
                if state.cancel_requested:
                    status = "cancelled"
                state.terminal = True

            stdout = stdout_bytes.decode(errors="replace")
            stderr = stderr_bytes.decode(errors="replace")

            response = SidecarExecuteResponse(
                execution_id=request.execution_id,
                status=status,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=duration,
                timed_out=timed_out,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
            )
            self._executions[request.execution_id] = response
            return response

        except asyncio.CancelledError:
            state.cancel_requested = True
            await asyncio.shield(self._stop_process(state))
            self._persist_cancelled(request.execution_id, started_at)
            raise
        except Exception:
            stop_task = asyncio.create_task(self._stop_process(state))
            try:
                await self._await_shielded_task(stop_task, propagate_cancellation=False)
            except BaseException:  # noqa: BLE001
                logger.warning("Failed to stop sandbox process after execution error")
            self._persist_failed(request.execution_id, started_at)
            raise

        finally:
            state.cleanup_task = asyncio.create_task(
                self._finalize(request.execution_id, state, network_name)
            )
            await self._await_shielded_task(state.cleanup_task)

    async def get_status(self, execution_id: str) -> SidecarStatusResponse | None:
        """Return the status of a previously submitted execution."""
        response = self._executions.get(execution_id)
        if response is None:
            return None
        return SidecarStatusResponse(
            execution_id=response.execution_id,
            status=response.status,
            returncode=response.returncode,
            stdout=response.stdout,
            stderr=response.stderr,
            duration_seconds=response.duration_seconds,
            timed_out=response.timed_out,
            stdout_truncated=response.stdout_truncated,
            stderr_truncated=response.stderr_truncated,
        )

    async def cancel(self, execution_id: str) -> bool:
        """Stop an active execution and persist an observable cancelled status.

        Returns ``False`` when ``execution_id`` was never submitted, so the
        caller can 404 the same way ``get_status`` does instead of reporting
        success for an id it has never seen. ``True`` covers both an active
        execution this call stops and one already finalized (whose
        ``_states`` entry ``_finalize`` already popped, but whose response
        still lives in ``_executions``).
        """
        async with self._registry_lock:
            state = self._states.get(execution_id)
        if state is None:
            return execution_id in self._executions
        async with state.lock:
            if state.terminal or (
                state.process is not None and state.process.returncode is not None
            ):
                return True
            state.cancel_requested = True
        await self._stop_process(state)
        self._persist_cancelled(execution_id, time.perf_counter())
        if state.task is not asyncio.current_task():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(state.task)
        await state.cleanup_done.wait()
        return True

    async def _finalize(self, execution_id: str, state: _ExecutionState, network_name: str) -> None:
        """Remove owned network state and retire the active execution entry."""
        if state.owns_network:
            try:
                await self._run_cmd(self._docker_binary, "network", "rm", network_name)
            except Exception:  # noqa: BLE001
                logger.warning("Failed to remove network %s", network_name)
        state.cleanup_done.set()
        async with self._registry_lock:
            if self._states.get(execution_id) is state:
                self._states.pop(execution_id, None)

    @staticmethod
    async def _await_shielded_task(
        task: asyncio.Task[None], *, propagate_cancellation: bool = True
    ) -> None:
        """Finish a cleanup task despite repeated caller cancellation."""
        current = asyncio.current_task()
        interrupted = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                interrupted = True
                if current is not None:
                    current.uncancel()
        await task
        if interrupted and propagate_cancellation:
            raise asyncio.CancelledError

    async def _stop_process(self, state: _ExecutionState) -> None:
        """Kill and reap an active child at most once."""
        async with state.lock:
            process = state.process
            if state.stop_started or process is None or process.returncode is not None:
                return
            state.stop_started = True
            process.kill()
            await process.wait()

    def _persist_cancelled(self, execution_id: str, started_at: float) -> SidecarExecuteResponse:
        """Persist an immutable cancelled execution result."""
        previous = self._executions.get(execution_id)
        response = SidecarExecuteResponse(
            execution_id=execution_id,
            status="cancelled",
            returncode=previous.returncode if previous else None,
            stdout=previous.stdout if previous else "",
            stderr=previous.stderr if previous else "",
            duration_seconds=time.perf_counter() - started_at,
            timed_out=previous.timed_out if previous else False,
            stdout_truncated=previous.stdout_truncated if previous else False,
            stderr_truncated=previous.stderr_truncated if previous else False,
        )
        self._executions[execution_id] = response
        state = self._states.get(execution_id)
        if state is not None:
            state.terminal = True
        return response

    def _persist_failed(self, execution_id: str, started_at: float) -> SidecarExecuteResponse:
        """Persist a generic failed execution result without leaking details."""
        response = SidecarExecuteResponse(
            execution_id=execution_id,
            status="failed",
            returncode=None,
            duration_seconds=time.perf_counter() - started_at,
        )
        self._executions[execution_id] = response
        state = self._states.get(execution_id)
        if state is not None:
            state.terminal = True
        return response

    async def _communicate_bounded(
        self,
        process: asyncio.subprocess.Process,
        stdin_bytes: bytes | None,
    ) -> tuple[bytes, bytes, bool, bool]:
        """Drain both output streams while retaining at most the configured cap."""
        stdout_stream = getattr(process, "stdout", None)
        stderr_stream = getattr(process, "stderr", None)
        if not hasattr(stdout_stream, "read") or not hasattr(stderr_stream, "read"):
            stdout, stderr = await process.communicate(input=stdin_bytes)
            stdout_raw = stdout.encode() if isinstance(stdout, str) else (stdout or b"")
            stderr_raw = stderr.encode() if isinstance(stderr, str) else (stderr or b"")
            return (
                stdout_raw[: self._max_output_bytes],
                stderr_raw[: self._max_output_bytes],
                len(stdout_raw) > self._max_output_bytes,
                len(stderr_raw) > self._max_output_bytes,
            )

        if stdin_bytes is not None and process.stdin is not None:
            process.stdin.write(stdin_bytes)
            await process.stdin.drain()
            process.stdin.close()

        stdout_task = asyncio.create_task(self._read_bounded(stdout_stream))
        stderr_task = asyncio.create_task(self._read_bounded(stderr_stream))
        try:
            await process.wait()
            (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
                stdout_task, stderr_task
            )
        except BaseException:
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        return stdout, stderr, stdout_truncated, stderr_truncated

    async def _read_bounded(self, stream: asyncio.StreamReader) -> tuple[bytes, bool]:
        """Drain a stream while retaining no more than the configured cap."""
        retained = bytearray()
        truncated = False
        while chunk := await stream.read(65_536):
            remaining = self._max_output_bytes - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(retained), truncated

    async def check_health(self) -> bool:
        """Verify Docker daemon is reachable."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker_binary,
                "info",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    async def _run_cmd(self, *args: str) -> tuple[bytes, bytes]:
        """Run a shell command and return (stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace")
            msg = f"Command {args} failed with rc={proc.returncode}: {stderr_text}"
            raise RuntimeError(msg)
        return stdout, stderr


__all__ = ["SidecarExecutor"]
