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
    def begin_once(
        self, action: ToolAction, context: ToolGovernanceContext
    ) -> ActionExecutionClaim: ...

    def complete(self, claim: ActionExecutionClaim, result: Any) -> ActionExecutionRecord: ...

    def mark_ambiguous(
        self,
        claim: ActionExecutionClaim,
        error: BaseException,
        *,
        close_claim: bool,
    ) -> ActionExecutionRecord: ...

    def fail_pre_effect(
        self, claim: ActionExecutionClaim, error: BaseException
    ) -> ActionExecutionRecord: ...

    def replay_or_raise(self, record: ActionExecutionRecord) -> Any: ...

    def reconcile_completed(
        self, action_key: str, result: Any, operator_ref: str
    ) -> ReconciliationRecord: ...

    def reconcile_no_effect(
        self, action_key: str, operator_ref: str
    ) -> ReconciliationRecord: ...


@runtime_checkable
class ApprovalRepository(Protocol):
    def get(self, approval_ref: str) -> ApprovalRecord: ...

    def replay_for(self, identity: Mapping[str, Any]) -> ApprovalRecord | None: ...

    def _claimed_replay(self, approval_ref: str, claim_token: str) -> ApprovalRecord | None: ...

    def _replay_for_claim(
        self,
        approval_ref: str,
        claim_token: str,
        identity: Mapping[str, Any],
    ) -> ApprovalRecord: ...

    def begin_once(
        self, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> tuple[ApprovalRecord, bool]: ...

    def decide(self, resolution: ApprovalResolution) -> ApprovalRecord: ...

    def consume(self, value: object) -> Any: ...

    def claim(self, ref: str, *, owner: str) -> ApprovalRecord: ...

    def finish(self, ref: str, claim_token: str) -> ApprovalRecord: ...

    def fail(self, ref: str, claim_token: str) -> ApprovalRecord: ...

    def ready(self, ref: str, checkpoint_id: str, interrupt_id: str) -> ApprovalRecord: ...

    def terminal(self, ref: str, state: ApprovalState) -> ApprovalRecord: ...

    def _claim(self, ref: str, *, owner: str) -> tuple[ApprovalRecord, bool]: ...

    def _acquire_resume_lock(self) -> Any: ...

    def _release_resume_lock(self, connection: Any) -> None: ...
