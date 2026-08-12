from __future__ import annotations

import asyncio
import json
from typing import Any

from zeroth.runtime.agents.tooling.base import (
    CLIToolOutputError,
    CLIToolProcessError,
    CLIToolTimeoutError,
    ExecutionPlacement,
    InModelT,
    OutModelT,
    Tool,
)

#: Deadline for a CLI tool that declares none of its own.
#:
#: ``timeout_seconds`` defaults to ``None`` on the tool and was passed straight
#: to ``asyncio.wait_for``, so a tool declared without an explicit timeout waited
#: forever on its child.
DEFAULT_CLI_TOOL_TIMEOUT_SECONDS = 120.0


class CLITool(Tool[InModelT, OutModelT]):
    def __init__(
        self,
        *,
        name: str,
        command: list[str],
        input_model: type[InModelT],
        output_model: type[OutModelT],
        input_mode: str = "json-stdin",
        output_mode: str = "json-stdout",
        description: str = "",
        capabilities: list[str] | None = None,
        side_effect: bool = False,
        timeout_seconds: float | None = None,
        requires_approval: bool = False,
        tags: list[str] | None = None,
        execution_placement: ExecutionPlacement = "local_only",
        remote_name: str | None = None,
    ) -> None:
        """Initialize CLITool."""
        if not command:
            raise ValueError("command must not be empty")
        if input_mode != "json-stdin":
            raise ValueError("Only json-stdin input_mode is supported in MVP")
        if output_mode != "json-stdout":
            raise ValueError("Only json-stdout output_mode is supported in MVP")

        super().__init__(
            name=name,
            description=description,
            input_model=input_model,
            output_model=output_model,
            capabilities=capabilities,
            side_effect=side_effect,
            timeout_seconds=timeout_seconds,
            requires_approval=requires_approval,
            tags=tags,
            executor_type="cli",
            execution_placement=execution_placement,
            remote_name=remote_name,
        )
        self.command = command
        self.input_mode = input_mode
        self.output_mode = output_mode

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """Kill *process* and reap it, tolerating a race with its own exit."""
        if process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:  # pragma: no cover - lost the exit race
            return
        # Shielded: on the cancellation path the surrounding task is already
        # being cancelled, and an unshielded await here would abandon the child
        # a second time.
        await asyncio.shield(process.wait())

    async def _execute_validated(self, ctx: Any, data: InModelT) -> Any:
        """Internal helper to execute validated."""
        payload = data.model_dump_json().encode("utf-8")
        process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # A bounded deadline always: ``timeout_seconds=None`` used to reach
        # ``wait_for`` unchanged, where it means wait forever.
        deadline = (
            self.timeout_seconds
            if self.timeout_seconds is not None
            else DEFAULT_CLI_TOOL_TIMEOUT_SECONDS
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=payload), timeout=deadline
            )
        except TimeoutError as exc:
            await self._terminate(process)
            raise CLIToolTimeoutError(f"CLI tool timed out for {self.name}") from exc
        except asyncio.CancelledError:
            # Cancellation is not a timeout, so it never entered the branch
            # above and the child outlived the cancelled task -- measured: the
            # process was still alive and had to be reaped by hand. Fail-fast
            # sibling cancellation and lease-loss drive cancellation both reach
            # here, so this is the common path, not the exotic one.
            await self._terminate(process)
            raise

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            raise CLIToolProcessError(
                f"CLI tool failed for {self.name}",
                exit_code=process.returncode,
                stderr=stderr_text,
                stdout=stdout_text,
            )

        try:
            return json.loads(stdout_text or "{}")
        except json.JSONDecodeError as exc:
            raise CLIToolOutputError(
                f"CLI tool returned invalid JSON for {self.name}: {stdout_text[:200]}"
            ) from exc
