"""Shared public types for cohort-aware token joins."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.tokens import CanonicalTokenOrder


class TokenJoinTransitionError(ValueError):
    """A join command contradicts the supplied durable cohort state."""


@dataclass(frozen=True, slots=True)
class JoinReducerInput:
    """One edge-labelled delivery in its durable canonical order."""

    source_token_id: str
    inbound_edge_id: str
    payload: JsonValue
    order: CanonicalTokenOrder


JoinReducer = Callable[[JoinConfig, tuple[JoinReducerInput, ...]], JsonValue]
FailureMode = Literal["fail_fast", "best_effort"]


__all__ = [
    "FailureMode",
    "JoinReducer",
    "JoinReducerInput",
    "TokenJoinTransitionError",
]
