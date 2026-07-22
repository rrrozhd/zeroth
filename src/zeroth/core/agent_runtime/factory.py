"""Legacy import path for :mod:`zeroth.runtime.agents.factory`.

``build_runners_for_deployment`` is deployment-fetch wiring and moved to
:mod:`zeroth.service.bootstrap.factory`; it is republished lazily so this
legacy path keeps working without putting the service domain on the
import path of the agent runtime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroth.runtime.agents.factory import (
    AgentRunnerFactoryError,
    build_agent_runners,
    tool_required_capabilities,
)

if TYPE_CHECKING:
    from zeroth.service.bootstrap.factory import (
        build_runners_for_deployment as build_runners_for_deployment,
    )

__all__ = [
    "AgentRunnerFactoryError",
    "build_agent_runners",
    "build_runners_for_deployment",
    "tool_required_capabilities",
]


def __getattr__(name: str) -> object:
    if name == "build_runners_for_deployment":
        from zeroth.service.bootstrap.factory import build_runners_for_deployment

        return build_runners_for_deployment
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
