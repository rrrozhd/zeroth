from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Union

from pydantic import BaseModel

from zeroth.core.governed.tools.base import ExecutionPlacement, InModelT, OutModelT, Tool

PythonReturn = Union[OutModelT, dict[str, Any], BaseModel]
PythonHandler = Callable[[Any, InModelT], Union[Awaitable[PythonReturn], PythonReturn]]


class PythonTool(Tool[InModelT, OutModelT]):
    def __init__(
        self,
        *,
        name: str,
        handler: PythonHandler,
        input_model: type[InModelT],
        output_model: type[OutModelT],
        description: str = "",
        capabilities: list[str] | None = None,
        side_effect: bool = False,
        timeout_seconds: float | None = None,
        requires_approval: bool = False,
        tags: list[str] | None = None,
        execution_placement: ExecutionPlacement = "local_only",
        remote_name: str | None = None,
    ) -> None:
        """Initialize PythonTool."""
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
            executor_type="python",
            execution_placement=execution_placement,
            remote_name=remote_name,
        )
        self._handler = handler

    async def _execute_validated(self, ctx: Any, data: InModelT) -> Any:
        """Internal helper to execute validated."""
        result = self._handler(ctx, data)
        if inspect.isawaitable(result):
            return await result
        return result


def tool(
    *,
    name: str,
    input_model: type[InModelT],
    output_model: type[OutModelT],
    description: str = "",
    capabilities: list[str] | None = None,
    side_effect: bool = False,
    timeout_seconds: float | None = None,
    requires_approval: bool = False,
    tags: list[str] | None = None,
    execution_placement: ExecutionPlacement = "local_only",
    remote_name: str | None = None,
) -> Callable[[PythonHandler], PythonTool[InModelT, OutModelT]]:
    """Tool."""
    def decorator(func: PythonHandler) -> PythonTool[InModelT, OutModelT]:
        """Decorator."""
        return PythonTool(
            name=name,
            description=description,
            handler=func,
            input_model=input_model,
            output_model=output_model,
            capabilities=capabilities,
            side_effect=side_effect,
            timeout_seconds=timeout_seconds,
            requires_approval=requires_approval,
            tags=tags,
            execution_placement=execution_placement,
            remote_name=remote_name,
        )

    return decorator
