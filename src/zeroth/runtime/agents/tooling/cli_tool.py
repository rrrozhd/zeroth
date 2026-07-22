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

    async def _execute_validated(self, ctx: Any, data: InModelT) -> Any:
        """Internal helper to execute validated."""
        payload = data.model_dump_json().encode("utf-8")
        process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(input=payload), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise CLIToolTimeoutError(f"CLI tool timed out for {self.name}") from exc

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
