"""LangGraph Check target without an eager LangGraph import."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class TargetInvocationError(RuntimeError):
    """The target could not satisfy the normalized invocation contract."""


@dataclass(slots=True)
class LangGraphCheckTarget:
    """Factories and stable input construction for one LangGraph target."""

    graph_factory: Callable[[Any], Any]
    checkpointer_factory: Callable[[Path], AbstractContextManager[Any]]
    case_input: Callable[[str], Mapping[str, Any]]
    invocation_config: Callable[[str, str], Mapping[str, Any]]
    entrypoint_digest: str = field(default="", init=False)

    def invoke(
        self,
        *,
        case: str,
        scenario_run_id: str,
        checkpointer_path: str | Path,
        callbacks: tuple[object, ...] = (),
    ) -> Mapping[str, Any]:
        if not isinstance(case, str) or not case.strip():
            raise TargetInvocationError("case must be a nonblank stable identifier")
        if not isinstance(scenario_run_id, str) or not scenario_run_id.strip():
            raise TargetInvocationError("scenario_run_id must be a nonblank logical identifier")
        path = Path(checkpointer_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        context_manager = self.checkpointer_factory(path)
        if not hasattr(context_manager, "__enter__"):
            raise TargetInvocationError("checkpointer_factory must return a context manager")
        with context_manager as checkpointer:
            graph = self.graph_factory(checkpointer)
            invoke = getattr(graph, "invoke", None)
            if not callable(invoke):
                raise TargetInvocationError("graph must expose invoke")
            config = dict(self.invocation_config(case, scenario_run_id))
            if callbacks:
                config["callbacks"] = [*config.get("callbacks", []), *callbacks]
            result = invoke(
                dict(self.case_input(case)),
                config=config,
            )
        if inspect.isawaitable(result):
            raise TargetInvocationError("sync invoke returned an awaitable")
        if not isinstance(result, Mapping):
            raise TargetInvocationError("graph result must be a mapping")
        return dict(result)
