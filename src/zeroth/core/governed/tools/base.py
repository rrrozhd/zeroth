from __future__ import annotations

import hashlib
import json
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ValidationError


InModelT = TypeVar("InModelT", bound=BaseModel)
OutModelT = TypeVar("OutModelT", bound=BaseModel)
ExecutionPlacement = Literal["local_only", "remote_only", "local_or_remote"]


class ToolError(Exception):
    """Base tool error."""


class ToolValidationError(ToolError):
    """Raised when input or output validation fails."""


class ToolExecutionError(ToolError):
    """Raised when tool execution fails."""


class CLIToolError(ToolExecutionError):
    """Base CLI tool error."""


class CLIToolProcessError(CLIToolError):
    def __init__(self, message: str, *, exit_code: int, stderr: str, stdout: str) -> None:
        """Initialize CLIToolProcessError."""
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr
        self.stdout = stdout


class CLIToolOutputError(CLIToolError):
    """Raised when CLI output is invalid."""


class CLIToolTimeoutError(CLIToolError):
    """Raised when CLI execution times out."""


class Tool(Generic[InModelT, OutModelT]):
    def __init__(
        self,
        *,
        name: str,
        version: str = "0.0.0",
        description: str = "",
        input_model: type[InModelT],
        output_model: type[OutModelT],
        capabilities: list[str] | None = None,
        side_effect: bool = False,
        timeout_seconds: float | None = None,
        requires_approval: bool = False,
        tags: list[str] | None = None,
        executor_type: str = "python",
        execution_placement: ExecutionPlacement = "local_only",
        remote_name: str | None = None,
    ) -> None:
        """Initialize Tool."""
        self.name = name
        self.version = version
        self.schema_fingerprint: str | None = None
        self.description = description
        self.input_model = input_model
        self.output_model = output_model
        self.capabilities = capabilities or []
        self.side_effect = side_effect
        self.timeout_seconds = timeout_seconds
        self.requires_approval = requires_approval
        self.tags = tags or []
        self.executor_type = executor_type
        self.execution_placement = execution_placement
        self.remote_name = remote_name or name

    async def execute(self, ctx: Any, data: Any) -> OutModelT:
        """Execute."""
        try:
            validated_input = self.input_model.model_validate(data)
        except ValidationError as exc:
            raise ToolValidationError(f"Input validation failed for {self.name}: {exc}") from exc

        try:
            output = await self._execute_validated(ctx, validated_input)
        except ToolError:
            raise
        except Exception as exc:  # pragma: no cover - defensive wrapper
            raise ToolExecutionError(f"Execution failed for {self.name}: {exc}") from exc

        try:
            return self.output_model.model_validate(output)
        except ValidationError as exc:
            raise ToolValidationError(f"Output validation failed for {self.name}: {exc}") from exc

    async def _execute_validated(self, ctx: Any, data: InModelT) -> Any:  # pragma: no cover - abstract
        """Internal helper to execute validated."""
        raise NotImplementedError

    def to_manifest(self) -> "ToolManifest":
        """Extract a read-only ToolManifest descriptor from this Tool.

        Computes schema fingerprint inline if the tool has not been registered
        (i.e. schema_fingerprint is None).
        """
        from zeroth.core.governed.tools.manifest import ToolManifest

        input_schema = self.input_model.model_json_schema()
        output_schema = self.output_model.model_json_schema()

        if self.schema_fingerprint is None:
            combined = json.dumps(
                {"input": input_schema, "output": output_schema}, sort_keys=True
            ).encode()
            fingerprint = hashlib.blake2b(combined, digest_size=16).hexdigest()
        else:
            fingerprint = self.schema_fingerprint

        return ToolManifest(
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=input_schema,
            output_schema=output_schema,
            schema_fingerprint=fingerprint,
            capabilities=list(self.capabilities),
            side_effect=self.side_effect,
            timeout_seconds=self.timeout_seconds,
            requires_approval=self.requires_approval,
            tags=list(self.tags),
            executor_type=self.executor_type,
            execution_placement=self.execution_placement,
            remote_name=self.remote_name,
        )

    @classmethod
    def from_cli(
        cls,
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
    ) -> "Tool[InModelT, OutModelT]":
        """From cli."""
        from zeroth.core.governed.tools.cli_tool import CLITool

        return CLITool(
            name=name,
            command=command,
            input_model=input_model,
            output_model=output_model,
            input_mode=input_mode,
            output_mode=output_mode,
            description=description,
            capabilities=capabilities,
            side_effect=side_effect,
            timeout_seconds=timeout_seconds,
            requires_approval=requires_approval,
            tags=tags,
            execution_placement=execution_placement,
            remote_name=remote_name,
        )
