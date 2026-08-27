"""LangChain-compatible taped tools with no live-callable escape path."""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolCallId
from pydantic import BaseModel, PrivateAttr, create_model

from zeroth.check.adapter.bindings import ToolRegistration
from zeroth.check.replay.matcher import ReplayMatcher


def _tool_content(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(
        result, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


class TapeBackedTool(BaseTool):
    """A tool whose only executable path is an approved ReplayMatcher result."""

    _matcher: ReplayMatcher = PrivateAttr()
    _schema_digest: str = PrivateAttr()

    def __init__(self, registration: ToolRegistration, matcher: ReplayMatcher) -> None:
        input_model = registration.input_model
        if not isinstance(input_model, type) or not issubclass(input_model, BaseModel):
            raise TypeError("replay requires a Pydantic tool input model")
        replay_model = create_model(
            f"{registration.name.title()}ReplayInput",
            __base__=input_model,
            tool_call_id=(Annotated[str, InjectedToolCallId], ...),
        )
        super().__init__(
            name=registration.name,
            description=f"Tape-backed Check tool {registration.name}",
            args_schema=replay_model,
        )
        self._matcher = matcher
        self._schema_digest = registration.input_schema_digest

    def _run(
        self,
        tool_call_id: Annotated[str, InjectedToolCallId],
        **arguments: Any,
    ) -> Any:
        return self._matcher.call(
            name=self.name,
            schema_digest=self._schema_digest,
            tool_call_id=tool_call_id,
            arguments=arguments,
        )

    async def _arun(
        self,
        tool_call_id: Annotated[str, InjectedToolCallId],
        **arguments: Any,
    ) -> Any:
        return self._run(tool_call_id=tool_call_id, **arguments)


class ReplayToolFactory:
    """Bind schema metadata discovered from target code to a taped tool."""

    def __init__(self, matcher: ReplayMatcher) -> None:
        self._matcher = matcher

    def bind_registration(self, registration: ToolRegistration) -> TapeBackedTool:
        self._matcher.validate_registration(registration.name, registration.input_schema_digest)
        return TapeBackedTool(registration, self._matcher)
