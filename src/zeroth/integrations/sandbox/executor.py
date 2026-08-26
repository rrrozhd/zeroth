"""Docker execution logic for the sandbox sidecar.

Runs untrusted code inside Docker containers with per-execution network
isolation. Each execution gets its own ``--internal`` Docker network to
prevent outbound access unless explicitly permitted.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
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
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarStatusResponse,
)
from zeroth.integrations.sandbox.staging import (
    WorkspaceStore,
    WorkspaceValidationCode,
    WorkspaceValidationError,
    resolve_helper_image,
    split_reauthored_tar,
    validate_workspace_id,
)

logger = logging.getLogger(__name__)


def _resolved_timeout(requested: float | None) -> float:
    """Return a finite, positive deadline for *requested*.

    ``timeout_seconds`` stays ``float | None`` on the request model because that
    model's constructor signature is pinned by the frozen protected-surface
    fixture. So the bound lives here instead: ``None`` means "nobody configured
    one", which resolves to the default rather than to ``asyncio.wait_for``'s
    ``None`` -- which means wait forever. A non-positive value is refused rather
    than quietly treated as immediate or infinite.
    """
    if requested is None:
        return DEFAULT_EXECUTION_TIMEOUT_SECONDS
    if not math.isfinite(requested) or requested <= 0:
        # inf reaches asyncio.wait_for and means "no deadline", which is exactly
        # the state this resolver exists to make unreachable. JSON carries the
        # Infinity literal and pydantic accepts it by default, so this is a
        # reachable input, not a theoretical one.
        raise ValueError("timeout_seconds must be positive and finite")
    return requested

#: Deadline for a ``docker`` helper invocation (network create/rm, ``info``).
#: These run outside the container's own bounded wait -- and the first of them
#: runs before it -- so they need a bound of their own.
DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS = 30.0

#: Deadline for the ZER-37 staging invocations (volume create/rm, the
#: populate helper fed a re-authored tar on stdin, the capture helper).
#: Longer than the plain docker-command deadline because populate moves up to
#: the full workspace byte cap through a container's stdin.
DEFAULT_STAGING_COMMAND_TIMEOUT_SECONDS = 60.0

#: Ceiling on a captured workspace output file. The bounded reader retains one
#: extra byte so overflow is observable; an overflowing capture drops the
#: payload and reports ``output_file_truncated`` instead.
DEFAULT_MAX_OUTPUT_FILE_BYTES = 16_777_216

#: How many terminal executions keep their captured stdout/stderr.
#:
#: ``_executions`` is never evicted, because it doubles as the permanent
#: duplicate-execution guard: ``execute`` refuses any id already present, and
#: that refusal is pinned by the hostile-workload suite. Dropping entries to
#: bound memory would therefore reopen replay. What is unbounded is the captured
#: *payload* -- up to ``max_output_bytes`` per execution -- so that is what ages
#: out, leaving the identity and its terminal status behind.
DEFAULT_RETAINED_PAYLOAD_EXECUTIONS = 256


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
    #: ZER-37: docker volumes this execution created; ``_finalize`` removes
    #: them on every path (success, timeout, cancel, error).
    owned_volumes: list[str] = field(default_factory=list)
    #: ZER-37: the claimed workspace spool file; deleted by ``_finalize``.
    claimed_spool: Path | None = None


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
        # Not constructor parameters: this class's signature is pinned by the
        # frozen protected-surface fixture. They are plain attributes so a test
        # can still narrow them without a public-surface change.
        self._docker_command_timeout_seconds = DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS
        self._retained_payload_executions = DEFAULT_RETAINED_PAYLOAD_EXECUTIONS
        # ZER-37 workspace staging, same idiom: plain attributes, wired by the
        # app (store) or narrowed by tests. ``_helper_image`` of ``None`` means
        # "resolve from the environment at use".
        self.workspace_store: WorkspaceStore | None = None
        self._staging_command_timeout_seconds = DEFAULT_STAGING_COMMAND_TIMEOUT_SECONDS
        self._max_output_file_bytes = DEFAULT_MAX_OUTPUT_FILE_BYTES
        self._helper_image: str | None = None
        self._executions: dict[str, SidecarExecuteResponse] = {}
        self._states: dict[str, _ExecutionState] = {}
        self._registry_lock = asyncio.Lock()

    async def execute(self, request: SidecarExecuteRequest) -> SidecarExecuteResponse:
        """Run a command in an isolated Docker container."""
        validate_docker_image_reference(request.image)
        if request.workspace_id is not None:
            # Boundary charset validation for every string that becomes a
            # docker volume name: the workspace id itself, and the execution
            # id the per-execution volume names embed. Raised before any
            # registration or side effect; ValueError maps to a 422 upstream.
            validate_workspace_id(request.workspace_id)
            validate_workspace_id(request.execution_id)
            if self.workspace_store is None:
                # No staging channel on this executor: the id cannot be known.
                raise WorkspaceValidationError(WorkspaceValidationCode.WORKSPACE_UNKNOWN)
        # Resolved up front so a rejected timeout fails before a container, a
        # network or a coroutine exists. Evaluating it inline at the wait_for
        # left self._communicate_bounded(...) constructed and never awaited.
        execution_timeout = _resolved_timeout(request.timeout_seconds)
        network_name = f"zeroth-sandbox-{request.execution_id}"
        main_volume = f"zeroth-ws-{request.execution_id}"
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
            # Step 0 (ZER-37): claim the staged workspace before any docker
            # side effect exists, so an unknown or consumed workspace fails
            # with nothing to unwind.
            workspace_spool: Path | None = None
            if request.workspace_id is not None:
                assert self.workspace_store is not None  # validated above
                workspace_spool = await self.workspace_store.claim(request.workspace_id)
                state.claimed_spool = workspace_spool

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

            # Step 1b (ZER-37): create and populate the per-execution named
            # volumes. Sources are volume NAMES, never host paths -- the "no
            # host mounts" pin in test_executor_argv.py stays true.
            volume_flags: list[str] = []
            if workspace_spool is not None:
                helper_image = validate_docker_image_reference(
                    self._helper_image or resolve_helper_image()
                )
                await self._run_staging_cmd(
                    self._docker_binary, "volume", "create", main_volume
                )
                state.owned_volumes.append(main_volume)
                ro_routes: list[tuple[str, str]] = []
                for index, ro_path in enumerate(request.read_only_paths):
                    ro_volume = f"{main_volume}-ro{index}"
                    await self._run_staging_cmd(
                        self._docker_binary, "volume", "create", ro_volume
                    )
                    state.owned_volumes.append(ro_volume)
                    ro_routes.append((ro_path, ro_volume))
                await self._populate_workspace_volumes(
                    workspace_spool, main_volume, ro_routes, helper_image
                )
                if state.cancel_requested:
                    return self._persist_cancelled(request.execution_id, started_at)
                volume_flags = ["-v", f"{main_volume}:/workspace"]
                for ro_path, ro_volume in ro_routes:
                    volume_flags.extend(["-v", f"{ro_volume}:/workspace/{ro_path}:ro"])

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
                *volume_flags,
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
                    # ``None`` here means wait forever, and the field defaults to
                    # ``None`` -- a body that simply omits it used to buy an
                    # unbounded container. Resolve it to a real deadline.
                    timeout=execution_timeout,
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

            # ZER-37: bring back the requested output file after the workload
            # exits. The payload rides the immediate execute response ONLY;
            # the persisted record stores None so the payload never joins the
            # ageing arithmetic, while the truncation marker survives.
            output_file_b64: str | None = None
            output_file_truncated = False
            if (
                workspace_spool is not None
                and request.capture_output_file is not None
                and status != "cancelled"
            ):
                output_file_b64, output_file_truncated = await self._capture_output_file(
                    main_volume, request.capture_output_file
                )

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
                output_file_b64=output_file_b64,
                output_file_truncated=output_file_truncated,
            )
            self._executions[request.execution_id] = response.model_copy(
                update={"output_file_b64": None}
            )
            self._retire_old_payloads()
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
            # The persisted record always stores None for the payload; only
            # the truncation marker replays through get_status.
            output_file_b64=response.output_file_b64,
            output_file_truncated=response.output_file_truncated,
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
        # ZER-37: owned volumes and the claimed spool go on every path --
        # _finalize runs from execute()'s ``finally``, so success, timeout,
        # cancellation and error all pass through here.
        for volume in state.owned_volumes:
            await self._remove_volume(volume)
        if state.claimed_spool is not None:
            with suppress(OSError):
                state.claimed_spool.unlink(missing_ok=True)
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
        self._retire_old_payloads()
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
        self._retire_old_payloads()
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

    async def _read_bounded(
        self, stream: asyncio.StreamReader, cap: int | None = None
    ) -> tuple[bytes, bool]:
        """Drain a stream while retaining no more than the given cap."""
        limit = self._max_output_bytes if cap is None else cap
        retained = bytearray()
        truncated = False
        while chunk := await stream.read(65_536):
            remaining = limit - len(retained)
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
            await self._communicate_deadlined(proc)
            return proc.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    def _retire_old_payloads(self) -> None:
        """Drop captured output from all but the newest retained executions.

        Identity and terminal status stay -- ``execute`` reads ``_executions`` to
        refuse a replayed id, and ``tests/security/test_hostile_workloads.py``
        pins that refusal, so evicting the entry itself would reopen replay. Only
        the payload ages out, which is the part that grows without bound
        (``max_output_bytes`` per execution, retained for the process lifetime).
        """
        retirable = len(self._executions) - self._retained_payload_executions
        if retirable <= 0:
            return
        for execution_id in list(self._executions)[:retirable]:
            response = self._executions[execution_id]
            if not response.stdout and not response.stderr:
                continue
            # The truncation flags carry this, because every sidecar model is
            # pinned by the frozen protected-surface fixture and no new field is
            # available. They already mean "what you are seeing is not the whole
            # output"; retirement is a second reason for that, alongside size.
            # Without them a retired execution is indistinguishable from one that
            # genuinely produced nothing.
            self._executions[execution_id] = response.model_copy(
                update={
                    "stdout": "",
                    "stderr": "",
                    "stdout_truncated": True,
                    "stderr_truncated": True,
                }
            )

    async def _communicate_deadlined(self, proc: Any) -> tuple[bytes, bytes]:
        """Await ``proc.communicate()`` under the docker-command deadline.

        The only bounded wait in this module used to be the one around the
        *container's own* execution. Every ``docker`` helper invocation -- network
        create, network rm, ``docker info`` -- awaited ``communicate()`` bare, and
        the first of those runs *before* the bounded wait is ever reached, so a
        wedged daemon hung the request with nothing able to time it out.
        """
        try:
            return await asyncio.wait_for(
                proc.communicate(), timeout=self._docker_command_timeout_seconds
            )
        except TimeoutError:
            if proc.returncode is None:
                proc.kill()
                with suppress(ProcessLookupError):
                    await proc.wait()
            raise
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise

    async def _run_cmd(self, *args: str) -> tuple[bytes, bytes]:
        """Run a shell command and return (stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await self._communicate_deadlined(proc)
        if proc.returncode != 0:
            stderr_text = stderr.decode(errors="replace")
            msg = f"Command {args} failed with rc={proc.returncode}: {stderr_text}"
            raise RuntimeError(msg)
        return stdout, stderr

    async def _run_staging_cmd(
        self, *args: str, stdin_path: Path | None = None
    ) -> tuple[bytes, bytes]:
        """Run a ZER-37 staging command under the (longer) staging deadline.

        ``stdin_path`` feeds a spooled, sidecar-authored tar straight to the
        child's stdin as an inherited file descriptor -- no byte ever pumps
        through the event loop. The failure message carries the argv and exit
        code but NOT stderr: helper stderr can echo member-derived strings.
        """
        stdin_file = stdin_path.open("rb") if stdin_path is not None else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=stdin_file,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._staging_command_timeout_seconds
                )
            except (TimeoutError, asyncio.CancelledError):
                if proc.returncode is None:
                    proc.kill()
                    with suppress(ProcessLookupError):
                        await proc.wait()
                raise
        finally:
            if stdin_file is not None:
                stdin_file.close()
        if proc.returncode != 0:
            msg = f"Staging command {args} failed with rc={proc.returncode}"
            raise RuntimeError(msg)
        return stdout, stderr

    async def _run_capture_cmd(
        self, *args: str, max_bytes: int
    ) -> tuple[bytes, bool, int | None]:
        """Run a capture helper, retaining at most ``max_bytes`` of stdout.

        Returns ``(stdout, overflowed, returncode)`` under the staging
        deadline; the child is killed and reaped on timeout or cancellation.
        """
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_task = asyncio.create_task(self._read_bounded(proc.stdout, cap=max_bytes))
        stderr_task = asyncio.create_task(self._read_bounded(proc.stderr, cap=4096))
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=self._staging_command_timeout_seconds
            )
            (data, overflowed), _ = await asyncio.gather(stdout_task, stderr_task)
        except BaseException:
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            if proc.returncode is None:
                proc.kill()
                with suppress(ProcessLookupError):
                    await proc.wait()
            raise
        return data, overflowed, proc.returncode

    async def _populate_workspace_volumes(
        self,
        spool_path: Path,
        main_volume: str,
        ro_routes: list[tuple[str, str]],
        helper_image: str,
    ) -> None:
        """Split the claimed spool into per-volume streams and extract them.

        One sequential pass re-authors the spool into one fresh tar per
        volume (main plus one per read-only prefix, prefix stripped so each
        archive is rooted at its own mountpoint). Each helper container then
        extracts its sidecar-authored stream from stdin -- it never parses a
        header the sidecar did not write.
        """
        with tempfile.TemporaryDirectory(dir=str(spool_path.parent)) as scratch:
            scratch_dir = Path(scratch)
            plan: list[tuple[str, Path, str]] = [("", scratch_dir / "main.tar", main_volume)]
            for index, (ro_path, ro_volume) in enumerate(ro_routes):
                plan.append((ro_path, scratch_dir / f"ro{index}.tar", ro_volume))

            def _split() -> None:
                streams = [(prefix, path.open("wb")) for prefix, path, _ in plan]
                try:
                    split_reauthored_tar(
                        spool_path, [(prefix, stream) for prefix, stream in streams]
                    )
                finally:
                    for _, stream in streams:
                        stream.close()

            await asyncio.to_thread(_split)
            for _, tar_path, volume in plan:
                await self._run_staging_cmd(
                    self._docker_binary,
                    "run",
                    "--rm",
                    "-i",
                    "--network=none",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "-v",
                    f"{volume}:/w",
                    helper_image,
                    "tar",
                    "-x",
                    "-f",
                    "-",
                    "-C",
                    "/w",
                    stdin_path=tar_path,
                )

    async def _capture_output_file(
        self, main_volume: str, relative_path: str
    ) -> tuple[str | None, bool]:
        """Read one file back from the main workspace volume, bounded.

        Returns ``(payload_b64, truncated)``. A missing file, a failed helper
        or a capture error yields ``(None, False)``; an overflow past the cap
        drops the payload and reports ``(None, True)``.
        """
        cap = self._max_output_file_bytes
        helper_image = validate_docker_image_reference(
            self._helper_image or resolve_helper_image()
        )
        try:
            data, overflowed, returncode = await self._run_capture_cmd(
                self._docker_binary,
                "run",
                "--rm",
                "--network=none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "-v",
                f"{main_volume}:/w:ro",
                helper_image,
                "cat",
                f"/w/{relative_path}",
                max_bytes=cap + 1,
            )
        except Exception:  # noqa: BLE001 - capture must not fail the execution
            logger.warning("Failed to capture output file from volume %s", main_volume)
            return None, False
        if returncode != 0:
            return None, False
        if overflowed or len(data) > cap:
            return None, True
        return base64.b64encode(data).decode("ascii"), False

    async def _remove_volume(self, volume: str) -> None:
        """Force-remove an owned volume, retrying once before giving up."""
        for attempt in (0, 1):
            try:
                await self._run_staging_cmd(
                    self._docker_binary, "volume", "rm", "-f", volume
                )
                return
            except Exception:  # noqa: BLE001
                if attempt:
                    logger.warning("Failed to remove workspace volume %s", volume)


__all__ = ["SidecarExecutor"]
