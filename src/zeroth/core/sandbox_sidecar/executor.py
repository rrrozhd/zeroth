"""Docker execution logic for the sandbox sidecar.

Runs untrusted code inside Docker containers with per-execution network
isolation. Each execution gets its own ``--internal`` Docker network to
prevent outbound access unless explicitly permitted.
"""

from __future__ import annotations

import asyncio
import logging
import time

from zeroth.core.execution_units.constraints import ResourceConstraints, build_docker_resource_flags
from zeroth.core.sandbox_sidecar.models import (
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarStatusResponse,
)

logger = logging.getLogger(__name__)


class SidecarExecutor:
    """Executes commands inside isolated Docker containers.

    Each execution creates a dedicated Docker network with ``--internal``
    to block outbound traffic, runs the container with resource limits,
    captures output, and tears down the network on completion.
    """

    def __init__(self, *, docker_binary: str = "docker") -> None:
        self._docker_binary = docker_binary
        self._executions: dict[str, SidecarExecuteResponse] = {}

    async def execute(self, request: SidecarExecuteRequest) -> SidecarExecuteResponse:
        """Run a command in an isolated Docker container."""
        network_name = f"zeroth-sandbox-{request.execution_id}"
        started_at = time.perf_counter()

        # Build resource constraints from request fields
        constraints = ResourceConstraints(
            cpu_cores=request.cpu_cores,
            memory_mb=request.memory_mb,
            max_processes=request.max_processes,
            network_access=request.network_access,
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

            # Step 2: Build docker run command
            resource_flags = build_docker_resource_flags(constraints)
            env_flags: list[str] = []
            for key, value in request.environment.items():
                env_flags.extend(["-e", f"{key}={value}"])

            cmd = [
                self._docker_binary,
                "run",
                "--rm",
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
                stdin_bytes = request.input_text.encode() if request.input_text else None
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=stdin_bytes),
                    timeout=request.timeout_seconds,
                )
                returncode = proc.returncode
            except TimeoutError:
                timed_out = True
                # Try to kill the process
                try:
                    proc.kill()  # type: ignore[possibly-undefined]
                    await proc.wait()  # type: ignore[possibly-undefined]
                except Exception:  # noqa: BLE001
                    pass
                stdout_bytes = b""
                stderr_bytes = b"Execution timed out"
                returncode = -1

            duration = time.perf_counter() - started_at
            status = "completed" if returncode == 0 else "failed"
            if timed_out:
                status = "failed"

            response = SidecarExecuteResponse(
                execution_id=request.execution_id,
                status=status,
                returncode=returncode,
                stdout=(
                    stdout_bytes.decode(errors="replace")
                    if isinstance(stdout_bytes, bytes)
                    else (stdout_bytes or "")
                ),
                stderr=(
                    stderr_bytes.decode(errors="replace")
                    if isinstance(stderr_bytes, bytes)
                    else (stderr_bytes or "")
                ),
                duration_seconds=duration,
                timed_out=timed_out,
            )
            self._executions[request.execution_id] = response
            return response

        finally:
            # Step 4: Cleanup network
            try:
                await self._run_cmd(
                    self._docker_binary,
                    "network",
                    "rm",
                    network_name,
                )
            except Exception:  # noqa: BLE001
                logger.warning("Failed to remove network %s", network_name)

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
        )

    async def cancel(self, execution_id: str) -> None:
        """Cancel a running execution (best-effort)."""
        response = self._executions.get(execution_id)
        if response is not None:
            self._executions[execution_id] = SidecarExecuteResponse(
                execution_id=execution_id,
                status="cancelled",
                returncode=response.returncode,
                stdout=response.stdout,
                stderr=response.stderr,
                duration_seconds=response.duration_seconds,
                timed_out=response.timed_out,
            )

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
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace")
            msg = f"Command {args} failed with rc={proc.returncode}: {stderr_text}"
            raise RuntimeError(msg)
        return stdout, stderr


__all__ = ["SidecarExecutor"]
