"""Public types and failures for structured-token loop transitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.tokens import LoopInstance
from zeroth.runtime.orchestration.token_join_models import JoinReducerInput


class TokenLoopTransitionError(ValueError):
    """A loop command contradicts the supplied durable token snapshot."""


class LoopReductionRecoveryError(TokenLoopTransitionError):
    """A durable loop reduction claim could not be safely recovered."""


class LoopReductionClaimChangedError(LoopReductionRecoveryError):
    """The observed loop reduction claim changed before a CAS operation won."""


class LoopReductionReleaseError(LoopReductionRecoveryError):
    """A failed reducer claim could not be returned to the barrier-ready state."""


@dataclass(frozen=True, slots=True)
class LoopReductionClaim:
    """Complete durable ownership identity for one iteration reduction."""

    claim_id: str
    owner_id: str
    attempt: int
    claimed_revision: int

    @classmethod
    def from_loop(cls, loop: LoopInstance) -> LoopReductionClaim:
        if (
            loop.reduction_claim_id is None
            or loop.reduction_claim_owner_id is None
            or loop.reduction_claim_revision is None
            or loop.reducer_fingerprint is None
        ):
            raise LoopReductionRecoveryError("loop has no complete active reduction claim")
        return cls(
            claim_id=loop.reduction_claim_id,
            owner_id=loop.reduction_claim_owner_id,
            attempt=loop.reduction_attempt,
            claimed_revision=loop.reduction_claim_revision,
        )


LoopReducer = Callable[[JoinConfig, tuple[JoinReducerInput, ...]], JsonValue]
FailureMode = Literal["fail_fast", "best_effort"]


__all__ = [
    "FailureMode",
    "LoopReducer",
    "LoopReductionClaim",
    "LoopReductionClaimChangedError",
    "LoopReductionRecoveryError",
    "LoopReductionReleaseError",
    "TokenLoopTransitionError",
]
