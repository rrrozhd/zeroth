"""Closed reason-code catalog and operator descriptions."""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    CONFIG_INVALID = "config_invalid"
    TAPE_UNAPPROVED = "tape_unapproved"
    TAPE_SCHEMA_INVALID = "tape_schema_invalid"
    NO_SIDE_EFFECTING_OCCURRENCE = "no_side_effecting_occurrence"
    TARGET_REBUILD_FAILED = "target_rebuild_failed"
    FAULT_NOT_OBSERVED = "fault_not_observed"
    INFRASTRUCTURE_FAILED = "infrastructure_failed"
    DUPLICATE_EFFECT = "duplicate_effect"
    LIVE_TOOL_ATTEMPTED = "live_tool_attempted"
    ACTION_IDENTITY_MISMATCH = "action_identity_mismatch"
    UNSAFE_RETRY = "unsafe_retry"
    CANCELLATION_SWALLOWED = "cancellation_swallowed"
    RESTART_REEXECUTED = "restart_reexecuted"
    REPLAY_MISMATCH_SAFETY = "replay_mismatch_safety"
    ORDINARY_QUORUM_MISSED = "ordinary_quorum_missed"
    USAGE_INCOMPLETE = "usage_incomplete"
    OPTIONAL_FAULT_INCONCLUSIVE = "optional_fault_inconclusive"


DESCRIPTIONS = {
    ReasonCode.CONFIG_INVALID: "The Check configuration is not valid V1 input.",
    ReasonCode.TAPE_UNAPPROVED: "The selected artifact is not an approved curated tape.",
    ReasonCode.TAPE_SCHEMA_INVALID: "The approved tape failed schema or digest validation.",
    ReasonCode.NO_SIDE_EFFECTING_OCCURRENCE: (
        "Full Check needs a curated side-effecting occurrence."
    ),
    ReasonCode.TARGET_REBUILD_FAILED: "The target could not be rebuilt from its public entrypoint.",
    ReasonCode.FAULT_NOT_OBSERVED: "A mandatory fault missed injection or recovery evidence.",
    ReasonCode.INFRASTRUCTURE_FAILED: "A bounded worker or durable harness prerequisite failed.",
    ReasonCode.DUPLICATE_EFFECT: "More than one external-effect marker was observed.",
    ReasonCode.LIVE_TOOL_ATTEMPTED: "Replay attempted to reach a live tool implementation.",
    ReasonCode.ACTION_IDENTITY_MISMATCH: "The candidate action identity differs from the tape.",
    ReasonCode.UNSAFE_RETRY: "An ambiguous or completed action was executed again.",
    ReasonCode.CANCELLATION_SWALLOWED: "Cancellation did not propagate to the caller.",
    ReasonCode.RESTART_REEXECUTED: "Restart added a second external-effect marker.",
    ReasonCode.REPLAY_MISMATCH_SAFETY: "A replay mismatch violated a safety invariant.",
    ReasonCode.ORDINARY_QUORUM_MISSED: "Fewer than two of three ordinary trajectories matched.",
    ReasonCode.USAGE_INCOMPLETE: "At least one model call lacks complete usage evidence.",
    ReasonCode.OPTIONAL_FAULT_INCONCLUSIVE: (
        "An optional fault did not produce conclusive evidence."
    ),
}
