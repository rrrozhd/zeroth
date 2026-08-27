"""Provider-neutral replay mismatch and quorum facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class MismatchClassification(StrEnum):
    INVALID = "invalid"
    BLOCK = "block"
    ORDINARY_MISMATCH = "ordinary_mismatch"


class MismatchReason(StrEnum):
    UNKNOWN_TOOL = "unknown_tool"
    EXTRA_CALL = "extra_call"
    EARLY_END = "early_end"
    CHANGED_ORDER = "changed_order"
    SCHEMA_DIGEST_MISMATCH = "schema_digest_mismatch"
    TOOL_CALL_ID_MISMATCH = "tool_call_id_mismatch"
    ARGUMENT_MISMATCH = "argument_mismatch"
    ACTION_IDENTITY_MISMATCH = "action_identity_mismatch"
    MISSING_RESULT = "missing_result"
    LIVE_TOOL_ATTEMPTED = "live_tool_attempted"
    DUPLICATE_SIDE_EFFECT = "duplicate_side_effect"


CLASSIFICATION = {
    MismatchReason.MISSING_RESULT: MismatchClassification.INVALID,
    MismatchReason.SCHEMA_DIGEST_MISMATCH: MismatchClassification.INVALID,
    MismatchReason.UNKNOWN_TOOL: MismatchClassification.BLOCK,
    MismatchReason.EXTRA_CALL: MismatchClassification.BLOCK,
    MismatchReason.TOOL_CALL_ID_MISMATCH: MismatchClassification.BLOCK,
    MismatchReason.ARGUMENT_MISMATCH: MismatchClassification.BLOCK,
    MismatchReason.ACTION_IDENTITY_MISMATCH: MismatchClassification.BLOCK,
    MismatchReason.LIVE_TOOL_ATTEMPTED: MismatchClassification.BLOCK,
    MismatchReason.DUPLICATE_SIDE_EFFECT: MismatchClassification.BLOCK,
    MismatchReason.EARLY_END: MismatchClassification.ORDINARY_MISMATCH,
    MismatchReason.CHANGED_ORDER: MismatchClassification.ORDINARY_MISMATCH,
}


@dataclass(frozen=True, slots=True)
class ReplayFact:
    reason: MismatchReason
    classification: MismatchClassification
    expected_fingerprint: str | None = None
    actual_fingerprint: str | None = None


class ReplayMismatchError(RuntimeError):
    def __init__(
        self,
        reason: MismatchReason,
        *,
        expected_fingerprint: str | None = None,
        actual_fingerprint: str | None = None,
    ) -> None:
        self.fact = ReplayFact(
            reason=reason,
            classification=CLASSIFICATION[reason],
            expected_fingerprint=expected_fingerprint,
            actual_fingerprint=actual_fingerprint,
        )
        super().__init__(f"{self.fact.classification.value}: {reason.value}")


@dataclass(frozen=True, slots=True)
class ReplayFinish:
    facts: tuple[ReplayFact, ...]
    observed_action_identities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuorumSummary:
    total_runs: int
    matching_runs: int
    required_matches: int
    quorum_met: bool


@dataclass(frozen=True, slots=True)
class ReplayRunEvidence:
    slot: int
    process_id: int
    checkpoint_path: Path
    action_repository_path: Path
    trajectory: bytes | None
    facts: tuple[ReplayFact, ...]
    usage_complete: bool
    action_repository_requested: bool
    full_check_eligible: bool
    infrastructure_error: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayBatch:
    runs: tuple[ReplayRunEvidence, ...]
    quorum: QuorumSummary
    invalid_slots: tuple[int, ...]
