from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class TransitionSpec:
    kind: str
    next_step: str | None = None
    router: str | None = None
    mapping: dict[str, str] | None = None
    allowed: list[str] | None = None


@dataclass
class GovernedStepSpec:
    name: str
    version: str = "0.0.0"
    tool: Any = None
    agent: Any = None
    required_artifacts: list[str] = field(default_factory=list)
    emitted_artifact: str | None = None
    approval_override: bool | None = None
    transition: TransitionSpec | None = None


@dataclass
class InterruptContract:
    ttl_seconds: int = 1800
    max_pending: int = 1


@dataclass
class ChannelSpec:
    name: str
    reducer: str = "replace"
    initial: Any = None


@dataclass
class GovernedFlowSpec:
    name: str
    steps: list[GovernedStepSpec]
    entry_step: str | None = None
    policies: list[Callable[..., Any]] = field(default_factory=list)
    skills: list[Any] = field(default_factory=list)
    channels: list[ChannelSpec] = field(default_factory=list)
    interrupts: InterruptContract = field(default_factory=InterruptContract)


def then(next_step: str) -> TransitionSpec:
    """Then."""
    return TransitionSpec(kind="then", next_step=next_step)


def end() -> TransitionSpec:
    """End."""
    return TransitionSpec(kind="end")


def branch(*, router: str, mapping: dict[str, str]) -> TransitionSpec:
    """Branch."""
    return TransitionSpec(kind="branch", router=router, mapping=dict(mapping))


def route_to(*, allowed: list[str]) -> TransitionSpec:
    """Route to."""
    return TransitionSpec(kind="route_to", allowed=list(allowed))
