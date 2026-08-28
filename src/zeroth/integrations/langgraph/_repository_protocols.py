"""Structural persistence seams used by governed LangGraph calls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from zeroth.integrations.langgraph._action_lifecycle import (
    ActionExecutionClaim,
    ActionExecutionRecord,
    ReconciliationRecord,
)
from zeroth.integrations.langgraph._approval_lifecycle import (
    ApprovalRecord,
    ApprovalResolution,
    ApprovalState,
)
from zeroth.integrations.langgraph._tool_types import ToolAction, ToolGovernanceContext


@runtime_checkable
class ActionExecutionRepository(Protocol):
    """Represent action execution repository state and behavior."""

    def begin_once(
        self, action: ToolAction, context: ToolGovernanceContext
    ) -> ActionExecutionClaim:
        """Begin once."""
        ...

    def complete(self, claim: ActionExecutionClaim, result: Any) -> ActionExecutionRecord:
        """Complete complete."""
        ...

    def mark_ambiguous(
        self,
        claim: ActionExecutionClaim,
        error: BaseException,
        *,
        close_claim: bool,
    ) -> ActionExecutionRecord:
        """Mark ambiguous."""
        ...

    def fail_pre_effect(
        self, claim: ActionExecutionClaim, error: BaseException
    ) -> ActionExecutionRecord:
        """Record failure for pre effect."""
        ...

    def replay_or_raise(self, record: ActionExecutionRecord) -> Any:
        """Replay or raise."""
        ...

    def reconcile_completed(
        self, action_key: str, result: Any, operator_ref: str
    ) -> ReconciliationRecord:
        """Reconcile completed."""
        ...

    def reconcile_no_effect(self, action_key: str, operator_ref: str) -> ReconciliationRecord:
        """Reconcile no effect."""
        ...


@runtime_checkable
class ApprovalRepository(Protocol):
    """Represent approval repository state and behavior."""

    def get(self, approval_ref: str) -> ApprovalRecord:
        """Retrieve get."""
        ...

    def replay_for(self, identity: Mapping[str, Any]) -> ApprovalRecord | None:
        """Replay for."""
        ...

    def _claimed_replay(self, approval_ref: str, claim_token: str) -> ApprovalRecord | None:
        """Implement the claimed replay boundary for this component."""
        ...

    def _replay_for_claim(
        self,
        approval_ref: str,
        claim_token: str,
        identity: Mapping[str, Any],
    ) -> ApprovalRecord:
        """Replay for claim."""
        ...

    def begin_once(
        self, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> tuple[ApprovalRecord, bool]:
        """Begin once."""
        ...

    def decide(self, resolution: ApprovalResolution) -> ApprovalRecord:
        """Implement the decide boundary for this component."""
        ...

    def consume(self, value: object) -> Any:
        """Consume consume."""
        ...

    def claim(self, ref: str, *, owner: str) -> ApprovalRecord:
        """Claim claim."""
        ...

    def finish(self, ref: str, claim_token: str) -> ApprovalRecord:
        """Finish finish."""
        ...

    def fail(self, ref: str, claim_token: str) -> ApprovalRecord:
        """Record failure for fail."""
        ...

    def ready(self, ref: str, checkpoint_id: str, interrupt_id: str) -> ApprovalRecord:
        """Mark ready."""
        ...

    def terminal(self, ref: str, state: ApprovalState) -> ApprovalRecord:
        """Move terminal."""
        ...

    def _claim(self, ref: str, *, owner: str) -> tuple[ApprovalRecord, bool]:
        """Claim claim."""
        ...

    def _acquire_resume_lock(self) -> Any:
        """Acquire resume lock."""
        ...

    def _release_resume_lock(self, connection: Any) -> None:
        """Release resume lock."""
        ...
