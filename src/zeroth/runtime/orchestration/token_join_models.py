"""Shared public types for cohort-aware token joins."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.tokens import CanonicalTokenOrder, JoinInstance, JoinLifecycleState


class TokenJoinTransitionError(ValueError):
    """A join command contradicts the supplied durable cohort state."""


class JoinReductionRecoveryError(TokenJoinTransitionError):
    """A durable reduction claim could not be safely recovered."""


class JoinReductionClaimChangedError(JoinReductionRecoveryError):
    """The observed reduction claim was replaced before this operation won CAS."""


class JoinReductionReleaseError(JoinReductionRecoveryError):
    """A failed in-process reducer claim could not be returned to READY."""


@dataclass(frozen=True, slots=True)
class JoinReductionClaim:
    """Complete durable ownership identity for one reducer evaluation attempt."""

    claim_id: str
    owner_id: str
    attempt: int
    claimed_revision: int

    @classmethod
    def from_join(cls, join: JoinInstance) -> JoinReductionClaim:
        if (
            join.lifecycle_state is not JoinLifecycleState.REDUCING
            or join.reduction_claim_id is None
            or join.reduction_claim_owner_id is None
            or join.reduction_claim_revision is None
        ):
            raise JoinReductionRecoveryError("join has no complete active reduction claim")
        return cls(
            claim_id=join.reduction_claim_id,
            owner_id=join.reduction_claim_owner_id,
            attempt=join.reduction_attempt,
            claimed_revision=join.reduction_claim_revision,
        )


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
    "JoinReductionClaim",
    "JoinReductionClaimChangedError",
    "JoinReductionRecoveryError",
    "JoinReductionReleaseError",
    "JoinReducer",
    "JoinReducerInput",
    "TokenJoinTransitionError",
]
