"""Durable contracts for the structured multi-token execution model.

The models in this module are persistence vocabulary only.  They deliberately
depend on Pydantic and the Python standard library, never on runtime scheduling
code, so repositories and model checkers can load token state in isolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

StableId = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
TokenId = StableId
ForkId = StableId
JoinInstanceId = StableId
LoopInstanceId = StableId
IterationFrameId = StableId
DispatchId = StableId
IdempotencyKey = StableId
NodeId = StableId
EdgeId = StableId
ObligationId = StableId
StateRevision = Annotated[int, Field(ge=0)]
CancellationGeneration = Annotated[int, Field(ge=0)]
RetryAttempt = Annotated[int, Field(ge=0)]
CreationOrdinal = Annotated[int, Field(ge=0)]
IterationIndex = Annotated[int, Field(ge=0)]


class SchedulingState(StrEnum):
    """A token's one exclusive scheduler location."""

    QUEUED = "queued"
    EXECUTING = "executing"
    JOIN_WAITING = "join_waiting"
    SETTLED = "settled"


class TokenLifecycleState(StrEnum):
    """Durable lifecycle independent of structured-scope ownership."""

    ACTIVE = "active"
    CANCELLING = "cancelling"
    SETTLED = "settled"


class ForkLifecycleState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class JoinLifecycleState(StrEnum):
    OPEN = "open"
    READY = "ready"
    REDUCING = "reducing"
    CANCELLED = "cancelled"
    CLOSED = "closed"


class LoopLifecycleState(StrEnum):
    RUNNING = "running"
    STOPPING = "stopping"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class IterationFrameState(StrEnum):
    ACTIVE = "active"
    BARRIER_READY = "barrier_ready"
    SETTLED = "settled"


class DispatchLifecycleState(StrEnum):
    EXECUTING = "executing"
    CANCELLATION_REQUESTED = "cancellation_requested"


class ForkObligationOutcome(StrEnum):
    JOINED = "joined"
    EXITED = "exited"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JoinObligationOutcome(StrEnum):
    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IterationMemberState(StrEnum):
    ACTIVE = "active"
    INTERNAL_COMPLETION = "internal_completion"
    BACK_EDGE_CONTINUATION = "back_edge_continuation"
    EXIT_DELIVERY = "exit_delivery"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LoopExitResolutionOutcome(StrEnum):
    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"


class _FrozenContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
    )


def _require_canonical_provenance(tag: tuple[ProvenanceFrame, ...], field: str) -> None:
    headers = tuple(frame.loop_header_node_id for frame in tag)
    if headers != tuple(sorted(headers)) or len(headers) != len(set(headers)):
        raise ValueError(f"{field} must use unique loop headers in canonical sorted order")


def _require_revision_window(created: int, updated: int) -> None:
    if updated < created:
        raise ValueError("updated_revision must be greater than or equal to created_revision")


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        frozen = MappingProxyType({key: _freeze_json(value[key]) for key in sorted(value)})
        return cast(JsonValue, frozen)
    if isinstance(value, list):
        return cast(JsonValue, tuple(_freeze_json(item) for item in value))
    return value


def _thaw_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class PayloadDelivery(_FrozenContract):
    """A wrapper that distinguishes a delivered JSON null from no delivery."""

    payload: JsonValue

    @field_validator("payload", mode="after")
    @classmethod
    def _freeze_payload(cls, value: JsonValue) -> JsonValue:
        return _freeze_json(value)

    @field_serializer("payload")
    def _serialize_payload(self, value: JsonValue) -> JsonValue:
        return _thaw_json(value)


class DeferredJoinDelivery(_FrozenContract):
    """A durable edge delivery held until an overlapping join frontier arrives."""

    delivery_id: StableId
    source_token_id: TokenId
    target_node_id: NodeId
    inbound_edge_id: EdgeId
    delivery: PayloadDelivery
    cancellation_generation: CancellationGeneration
    created_revision: StateRevision


class ProvenanceFrame(_FrozenContract):
    loop_header_node_id: NodeId
    iteration_index: IterationIndex


class ForkLineageFrame(_FrozenContract):
    """One root-to-leaf entry in a token's durable fork lineage."""

    fork_id: ForkId
    parent_fork_id: ForkId | None = None
    child_ordinal: CreationOrdinal
    join_instance_id: JoinInstanceId | None = None

    @model_validator(mode="after")
    def _reject_self_parent(self) -> ForkLineageFrame:
        if self.parent_fork_id == self.fork_id:
            raise ValueError("a fork lineage frame cannot parent itself")
        return self


class IterationMembership(_FrozenContract):
    """A token's membership in one frame of a nested loop-owner chain."""

    loop_instance_id: LoopInstanceId
    parent_loop_instance_id: LoopInstanceId | None = None
    iteration_frame_id: IterationFrameId
    loop_header_node_id: NodeId
    iteration_index: IterationIndex

    @model_validator(mode="after")
    def _reject_self_parent(self) -> IterationMembership:
        if self.parent_loop_instance_id == self.loop_instance_id:
            raise ValueError("an iteration membership cannot parent itself")
        return self


class TokenEnvelope(_FrozenContract):
    """The complete, replayable state carried by one control-flow token."""

    token_id: TokenId
    parent_token_id: TokenId | None = None
    continuation_parent_token_ids: tuple[TokenId, ...] = ()
    provenance_tag: tuple[ProvenanceFrame, ...] = ()
    current_node_id: NodeId
    causal_inbound_edge_id: EdgeId | None = None
    payload: JsonValue
    retry_attempt: RetryAttempt = 0
    lifecycle_state: TokenLifecycleState
    scheduling_state: SchedulingState
    fork_lineage: tuple[ForkLineageFrame, ...] = ()
    iteration_memberships: tuple[IterationMembership, ...] = ()
    cancellation_generation: CancellationGeneration = 0
    cancellation_acknowledged_generation: CancellationGeneration | None = None
    state_revision: StateRevision
    settled_revision: StateRevision | None = None

    @field_validator("payload", mode="after")
    @classmethod
    def _freeze_payload(cls, value: JsonValue) -> JsonValue:
        return _freeze_json(value)

    @field_serializer("payload")
    def _serialize_payload(self, value: JsonValue) -> JsonValue:
        return _thaw_json(value)

    @model_validator(mode="after")
    def _validate_envelope(self) -> TokenEnvelope:
        if self.parent_token_id == self.token_id:
            raise ValueError("parent_token_id cannot refer to the token itself")
        if self.parent_token_id is not None and self.continuation_parent_token_ids:
            raise ValueError("a token cannot have both parent_token_id and continuation parentage")
        parents = self.continuation_parent_token_ids
        if len(parents) != len(set(parents)):
            raise ValueError("continuation_parent_token_ids must be unique")
        if self.token_id in parents:
            raise ValueError("continuation parentage cannot contain the token itself")

        _require_canonical_provenance(self.provenance_tag, "provenance_tag")

        fork_ids = tuple(frame.fork_id for frame in self.fork_lineage)
        if len(fork_ids) != len(set(fork_ids)):
            raise ValueError("fork_lineage cannot contain the same fork twice")
        for index, frame in enumerate(self.fork_lineage):
            expected_parent = None if index == 0 else self.fork_lineage[index - 1].fork_id
            if frame.parent_fork_id != expected_parent:
                raise ValueError("fork_lineage contains an orphan or missing parent fork")

        loop_ids = tuple(item.loop_instance_id for item in self.iteration_memberships)
        frame_ids = tuple(item.iteration_frame_id for item in self.iteration_memberships)
        if len(loop_ids) != len(set(loop_ids)) or len(frame_ids) != len(set(frame_ids)):
            raise ValueError("iteration_memberships must name unique loop and frame owners")
        for index, membership in enumerate(self.iteration_memberships):
            expected_parent = (
                None if index == 0 else self.iteration_memberships[index - 1].loop_instance_id
            )
            if membership.parent_loop_instance_id != expected_parent:
                raise ValueError("iteration_memberships contain an orphan or missing loop parent")
        tag_members = {
            frame.loop_header_node_id: frame.iteration_index for frame in self.provenance_tag
        }
        owner_members = {
            item.loop_header_node_id: item.iteration_index for item in self.iteration_memberships
        }
        if tag_members != owner_members:
            raise ValueError("provenance_tag must exactly match the token's iteration_memberships")

        settled = self.scheduling_state is SchedulingState.SETTLED
        if settled != (self.lifecycle_state is TokenLifecycleState.SETTLED):
            raise ValueError("SETTLED scheduling and lifecycle states must be present together")
        if self.lifecycle_state is TokenLifecycleState.CANCELLING and (
            self.scheduling_state is not SchedulingState.EXECUTING
        ):
            raise ValueError("a CANCELLING token must remain in EXECUTING scheduling state")
        if (
            self.lifecycle_state is TokenLifecycleState.CANCELLING
            and self.cancellation_generation == 0
        ):
            raise ValueError("a CANCELLING token requires a positive cancellation_generation")
        if settled != (self.settled_revision is not None):
            raise ValueError("settled_revision is required exactly when a token is SETTLED")
        if self.settled_revision is not None and self.settled_revision > self.state_revision:
            raise ValueError("settled_revision cannot exceed state_revision")

        acknowledged = self.cancellation_acknowledged_generation
        if acknowledged is not None:
            if acknowledged == 0 or self.cancellation_generation == 0:
                raise ValueError(
                    "cancellation acknowledgement requires a positive cancellation_generation"
                )
            if acknowledged != self.cancellation_generation:
                raise ValueError(
                    "cancellation_acknowledged_generation must equal the current generation"
                )
            if not settled:
                raise ValueError("only a settled token may acknowledge cancellation")
        return self


class ForkChild(_FrozenContract):
    token_id: TokenId
    creation_ordinal: CreationOrdinal


class ForkObligation(_FrozenContract):
    obligation_id: ObligationId
    fork_id: ForkId
    child_token_id: TokenId
    child_ordinal: CreationOrdinal
    outcome: ForkObligationOutcome | None = None
    join_instance_id: JoinInstanceId | None = None
    exit_edge_id: EdgeId | None = None
    settled_revision: StateRevision | None = None

    @model_validator(mode="after")
    def _validate_settlement(self) -> ForkObligation:
        settled = self.outcome is not None
        if settled != (self.settled_revision is not None):
            raise ValueError("outcome and settled_revision must be recorded together")
        if self.outcome is ForkObligationOutcome.JOINED:
            if self.join_instance_id is None or self.exit_edge_id is not None:
                raise ValueError("JOINED requires only join_instance_id")
        elif self.outcome is ForkObligationOutcome.EXITED:
            if self.exit_edge_id is None or self.join_instance_id is not None:
                raise ValueError("EXITED requires only exit_edge_id")
        elif self.join_instance_id is not None or self.exit_edge_id is not None:
            raise ValueError("only JOINED or EXITED obligations may name a settlement target")
        return self


class ForkInstance(_FrozenContract):
    fork_id: ForkId
    parent_token_id: TokenId
    parent_fork_id: ForkId | None = None
    children: Annotated[tuple[ForkChild, ...], Field(min_length=1)]
    obligations: Annotated[tuple[ForkObligation, ...], Field(min_length=1)]
    outstanding_child_count: Annotated[int, Field(ge=0)]
    lifecycle_state: ForkLifecycleState
    created_revision: StateRevision
    updated_revision: StateRevision
    closed_revision: StateRevision | None = None

    @model_validator(mode="after")
    def _validate_fork(self) -> ForkInstance:
        if self.parent_fork_id == self.fork_id:
            raise ValueError("a fork cannot parent itself")
        _require_revision_window(self.created_revision, self.updated_revision)

        ordinals = tuple(child.creation_ordinal for child in self.children)
        if ordinals != tuple(range(len(self.children))):
            raise ValueError("fork child creation ordinals must be contiguous and canonical")
        child_ids = tuple(child.token_id for child in self.children)
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("fork child token IDs must be unique")

        obligation_keys = tuple(
            (item.child_token_id, item.child_ordinal) for item in self.obligations
        )
        child_keys = tuple((item.token_id, item.creation_ordinal) for item in self.children)
        if obligation_keys != child_keys:
            raise ValueError("fork obligations must exactly match the ordered child set")
        obligation_ids = tuple(item.obligation_id for item in self.obligations)
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValueError("fork obligation IDs must be unique")
        if any(item.fork_id != self.fork_id for item in self.obligations):
            raise ValueError("every fork obligation fork_id must match its ForkInstance")
        unsettled = sum(item.outcome is None for item in self.obligations)
        if self.outstanding_child_count != unsettled:
            raise ValueError("outstanding_child_count must equal unsettled obligations")
        for item in self.obligations:
            if item.settled_revision is not None and not (
                self.created_revision <= item.settled_revision <= self.updated_revision
            ):
                raise ValueError(
                    "obligation settlement revision is outside the fork revision window"
                )

        closed = self.lifecycle_state is ForkLifecycleState.CLOSED
        if closed != (self.closed_revision is not None):
            raise ValueError("closed_revision is required exactly when a fork is CLOSED")
        if closed and self.outstanding_child_count:
            raise ValueError("a CLOSED fork cannot have outstanding children")
        if not closed and not self.outstanding_child_count:
            raise ValueError("an OPEN fork must have an outstanding child")
        if self.closed_revision is not None and not (
            self.created_revision <= self.closed_revision <= self.updated_revision
        ):
            raise ValueError("closed_revision is outside the fork revision window")
        return self


class JoinObligation(_FrozenContract):
    obligation_id: ObligationId
    join_instance_id: JoinInstanceId
    fork_id: ForkId
    source_token_id: TokenId
    source_dispatch_id: DispatchId | None = None
    source_dispatch_attempt: RetryAttempt | None = None
    source_cancellation_generation: CancellationGeneration | None = None
    source_reported_outcome: JoinObligationOutcome | None = None
    source_reported_delivery: PayloadDelivery | None = None
    arrival_command_fingerprint: str | None = None
    inbound_edge_id: EdgeId
    child_ordinal: CreationOrdinal
    outcome: JoinObligationOutcome | None = None
    delivery: PayloadDelivery | None = None
    settled_revision: StateRevision | None = None

    @model_validator(mode="after")
    def _validate_settlement(self) -> JoinObligation:
        settled = self.outcome is not None
        if settled != (self.settled_revision is not None):
            raise ValueError("outcome and settled_revision must be recorded together")
        if self.outcome is JoinObligationOutcome.DELIVERED:
            if self.delivery is None:
                raise ValueError("DELIVERED join obligations require a delivery")
        elif self.delivery is not None:
            raise ValueError("only a DELIVERED join obligation may carry a delivery")
        if not settled and self.source_dispatch_id is not None:
            raise ValueError("an unsettled join obligation cannot name a consumed dispatch")
        dispatch_metadata = (
            self.source_dispatch_id,
            self.source_dispatch_attempt,
            self.source_cancellation_generation,
        )
        if any(item is not None for item in dispatch_metadata) and not all(
            item is not None for item in dispatch_metadata
        ):
            raise ValueError("consumed dispatch identity fields must be recorded together")
        if self.source_dispatch_id is None and (
            self.source_reported_outcome is not None or self.source_reported_delivery is not None
        ):
            raise ValueError("reported dispatch result requires consumed dispatch identity")
        if self.source_reported_outcome is JoinObligationOutcome.DELIVERED:
            if self.source_reported_delivery is None:
                raise ValueError("reported DELIVERED result requires its delivery")
        elif self.source_reported_delivery is not None:
            raise ValueError("only a reported DELIVERED result may carry delivery")
        return self


class JoinInstance(_FrozenContract):
    join_instance_id: JoinInstanceId
    fork_id: ForkId
    target_node_id: NodeId
    provenance_tag: tuple[ProvenanceFrame, ...] = ()
    iteration_memberships: tuple[IterationMembership, ...] = ()
    obligations: Annotated[tuple[JoinObligation, ...], Field(min_length=1)]
    failure_mode: Literal["fail_fast", "best_effort"] = "fail_fast"
    lifecycle_state: JoinLifecycleState
    continuation_token_id: TokenId | None = None
    consumed_parent_token_ids: tuple[TokenId, ...] = ()
    reducer_fingerprint: str | None = None
    reduction_attempt: Annotated[int, Field(ge=0)] = 0
    reduction_claim_id: str | None = None
    reduction_claim_owner_id: str | None = None
    reduction_claim_revision: StateRevision | None = None
    completed_reduction_claim_id: str | None = None
    completed_reduction_claim_owner_id: str | None = None
    completed_reduction_attempt: Annotated[int, Field(ge=1)] | None = None
    completed_reduction_claim_revision: StateRevision | None = None
    created_revision: StateRevision
    updated_revision: StateRevision
    closed_revision: StateRevision | None = None

    @property
    def delivered_obligation_count(self) -> int:
        return sum(item.outcome is JoinObligationOutcome.DELIVERED for item in self.obligations)

    @model_validator(mode="after")
    def _validate_join(self) -> JoinInstance:
        _require_revision_window(self.created_revision, self.updated_revision)
        _require_canonical_provenance(self.provenance_tag, "provenance_tag")
        loop_ids = tuple(item.loop_instance_id for item in self.iteration_memberships)
        frame_ids = tuple(item.iteration_frame_id for item in self.iteration_memberships)
        if len(loop_ids) != len(set(loop_ids)) or len(frame_ids) != len(set(frame_ids)):
            raise ValueError("join iteration_memberships must name unique owners")
        for index, membership in enumerate(self.iteration_memberships):
            expected_parent = (
                None if index == 0 else self.iteration_memberships[index - 1].loop_instance_id
            )
            if membership.parent_loop_instance_id != expected_parent:
                raise ValueError("join iteration_memberships contain an orphan loop parent")
        tag_members = {
            frame.loop_header_node_id: frame.iteration_index for frame in self.provenance_tag
        }
        owner_members = {
            item.loop_header_node_id: item.iteration_index for item in self.iteration_memberships
        }
        if tag_members != owner_members:
            raise ValueError("join provenance_tag must exactly match its iteration memberships")
        if any(item.join_instance_id != self.join_instance_id for item in self.obligations):
            raise ValueError("every obligation join_instance_id must match its JoinInstance")
        if any(item.fork_id != self.fork_id for item in self.obligations):
            raise ValueError("every obligation fork_id must match its JoinInstance")
        obligation_ids = tuple(item.obligation_id for item in self.obligations)
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValueError("join obligation IDs must be unique")
        token_edge_pairs = tuple(
            (item.source_token_id, item.inbound_edge_id) for item in self.obligations
        )
        if len(token_edge_pairs) != len(set(token_edge_pairs)):
            raise ValueError("join obligations must name unique token/edge resolutions")
        source_token_ids = tuple(item.source_token_id for item in self.obligations)
        if len(source_token_ids) != len(set(source_token_ids)):
            raise ValueError("join obligations must use unique source_token_id values")
        ordinals = tuple(item.child_ordinal for item in self.obligations)
        if ordinals != tuple(sorted(ordinals)) or len(ordinals) != len(set(ordinals)):
            raise ValueError("join obligations must use unique canonical child ordinals")
        for item in self.obligations:
            if item.settled_revision is not None and not (
                self.created_revision <= item.settled_revision <= self.updated_revision
            ):
                raise ValueError(
                    "obligation settlement revision is outside the join revision window"
                )

        all_settled = all(item.outcome is not None for item in self.obligations)
        any_delivered = self.delivered_obligation_count > 0
        claim_fields = (
            self.reduction_claim_id,
            self.reduction_claim_owner_id,
            self.reduction_claim_revision,
        )
        if any(item is not None for item in claim_fields) and not all(
            item is not None for item in claim_fields
        ):
            raise ValueError("reduction claim identity fields must be recorded together")
        completed_claim_fields = (
            self.completed_reduction_claim_id,
            self.completed_reduction_claim_owner_id,
            self.completed_reduction_attempt,
            self.completed_reduction_claim_revision,
        )
        if any(item is not None for item in completed_claim_fields) and not all(
            item is not None for item in completed_claim_fields
        ):
            raise ValueError("completed reduction claim fields must be recorded together")
        if self.lifecycle_state is not JoinLifecycleState.CLOSED and any(
            item is not None for item in completed_claim_fields
        ):
            raise ValueError("only a CLOSED join may retain completed claim identity")
        if len(self.consumed_parent_token_ids) != len(set(self.consumed_parent_token_ids)):
            raise ValueError("consumed_parent_token_ids must be unique")
        if self.continuation_token_id in self.consumed_parent_token_ids:
            raise ValueError("a continuation token cannot reuse a consumed parent token ID")
        if self.lifecycle_state is JoinLifecycleState.OPEN:
            if all_settled:
                raise ValueError("an OPEN join must have an unsettled obligation")
            if (
                self.continuation_token_id is not None
                or self.consumed_parent_token_ids
                or self.reducer_fingerprint is not None
                or self.reduction_claim_id is not None
                or self.reduction_claim_owner_id is not None
                or self.reduction_claim_revision is not None
                or self.reduction_attempt != 0
            ):
                raise ValueError("an OPEN join cannot have continuation state")
        elif self.lifecycle_state is JoinLifecycleState.READY:
            if not all_settled or not any_delivered:
                raise ValueError("a READY join requires all obligations settled and one delivered")
            if (
                self.continuation_token_id is not None
                or self.consumed_parent_token_ids
                or self.reducer_fingerprint is not None
                or self.reduction_claim_id is not None
                or self.reduction_claim_owner_id is not None
                or self.reduction_claim_revision is not None
            ):
                raise ValueError("a READY join cannot have continuation state")
        elif self.lifecycle_state is JoinLifecycleState.REDUCING:
            if not all_settled or not any_delivered:
                raise ValueError("a REDUCING join requires settled delivered input")
            if self.continuation_token_id is not None or self.consumed_parent_token_ids:
                raise ValueError("a REDUCING join cannot consume parents early")
            if self.reducer_fingerprint is None or not all(
                item is not None for item in claim_fields
            ):
                raise ValueError("a REDUCING join requires reducer and claim identity")
            if self.reduction_attempt < 1:
                raise ValueError("a REDUCING join requires a positive reduction attempt")
            if self.reduction_claim_revision != self.updated_revision:
                raise ValueError("a REDUCING join claim revision must match join revision")
        elif self.lifecycle_state is JoinLifecycleState.CANCELLED:
            if not all_settled:
                raise ValueError("a CANCELLED join cannot have unsettled obligations")
            expected_parents = tuple(item.source_token_id for item in self.obligations)
            if self.consumed_parent_token_ids != expected_parents:
                raise ValueError("a CANCELLED join must record every consumed parent")
            if self.continuation_token_id is not None:
                raise ValueError("a CANCELLED join cannot publish a continuation")
            if any(item is not None for item in (*claim_fields, *completed_claim_fields)):
                raise ValueError("a CANCELLED join cannot retain reduction claim state")
        else:
            if not all_settled:
                raise ValueError("a CLOSED join cannot have unsettled obligations")
            expected_parents = tuple(item.source_token_id for item in self.obligations)
            if self.consumed_parent_token_ids != expected_parents:
                raise ValueError(
                    "a CLOSED join must record every consumed token in canonical order"
                )
            if any_delivered != (self.continuation_token_id is not None):
                raise ValueError(
                    "a CLOSED join has a continuation exactly when an obligation delivered"
                )
            if any(item is not None for item in claim_fields):
                raise ValueError("a CLOSED join cannot retain a reduction claim")
            if self.completed_reduction_attempt is not None:
                if self.completed_reduction_attempt != self.reduction_attempt:
                    raise ValueError("completed claim attempt must match reduction_attempt")
                if self.completed_reduction_claim_revision is None or not (
                    self.created_revision
                    <= self.completed_reduction_claim_revision
                    <= self.updated_revision
                ):
                    raise ValueError("completed claim revision is outside the join window")

        closed = self.lifecycle_state in {
            JoinLifecycleState.CANCELLED,
            JoinLifecycleState.CLOSED,
        }
        if closed != (self.closed_revision is not None):
            raise ValueError("closed_revision is required exactly when a join is CLOSED")
        if self.closed_revision is not None and not (
            self.created_revision <= self.closed_revision <= self.updated_revision
        ):
            raise ValueError("closed_revision is outside the join revision window")
        return self


class CanonicalTokenOrder(_FrozenContract):
    """Persisted ordering data used by loop and join reducers."""

    iteration_index: IterationIndex
    fork_lineage: tuple[ForkLineageFrame, ...] = ()
    child_ordinal: CreationOrdinal
    token_id: TokenId

    @model_validator(mode="after")
    def _validate_lineage(self) -> CanonicalTokenOrder:
        fork_ids = tuple(frame.fork_id for frame in self.fork_lineage)
        if len(fork_ids) != len(set(fork_ids)):
            raise ValueError("canonical fork lineage cannot contain duplicate forks")
        for index, frame in enumerate(self.fork_lineage):
            expected_parent = None if index == 0 else self.fork_lineage[index - 1].fork_id
            if frame.parent_fork_id != expected_parent:
                raise ValueError("canonical fork lineage contains an orphan parent")
        if self.fork_lineage and self.fork_lineage[-1].child_ordinal != self.child_ordinal:
            raise ValueError("child_ordinal must match the innermost fork lineage frame")
        return self

    def sort_key(self) -> tuple[object, ...]:
        lineage = tuple((frame.fork_id, frame.child_ordinal) for frame in self.fork_lineage)
        return (self.iteration_index, lineage, self.child_ordinal, self.token_id)


class IterationMember(_FrozenContract):
    token_id: TokenId
    state: IterationMemberState
    causal_edge_id: EdgeId | None = None
    settlement_command_fingerprint: StableId | None = None
    settled_revision: StateRevision | None = None

    @model_validator(mode="after")
    def _validate_state(self) -> IterationMember:
        active = self.state is IterationMemberState.ACTIVE
        if active == (self.settled_revision is not None):
            raise ValueError("ACTIVE members are unsettled; all other member states are settled")
        if active and self.settlement_command_fingerprint is not None:
            raise ValueError("an ACTIVE member cannot retain a settlement command fingerprint")
        needs_edge = self.state in {
            IterationMemberState.BACK_EDGE_CONTINUATION,
            IterationMemberState.EXIT_DELIVERY,
        }
        if needs_edge and self.causal_edge_id is None:
            raise ValueError("back-edge and exit outcomes require causal_edge_id")
        return self


class IterationContinuationDelivery(_FrozenContract):
    token_id: TokenId
    back_edge_id: EdgeId
    delivery: PayloadDelivery
    canonical_order: CanonicalTokenOrder
    settled_revision: StateRevision

    @model_validator(mode="after")
    def _validate_identity(self) -> IterationContinuationDelivery:
        if self.canonical_order.token_id != self.token_id:
            raise ValueError("canonical_order token_id must match the continuation token")
        return self


class IterationFrame(_FrozenContract):
    iteration_frame_id: IterationFrameId
    loop_instance_id: LoopInstanceId
    iteration_index: IterationIndex
    members: Annotated[tuple[IterationMember, ...], Field(min_length=1)]
    continuation_deliveries: tuple[IterationContinuationDelivery, ...] = ()
    state: IterationFrameState
    created_revision: StateRevision
    updated_revision: StateRevision
    settled_revision: StateRevision | None = None

    @property
    def active_member_token_ids(self) -> tuple[TokenId, ...]:
        return tuple(
            member.token_id
            for member in self.members
            if member.state is IterationMemberState.ACTIVE
        )

    @model_validator(mode="after")
    def _validate_frame(self) -> IterationFrame:
        _require_revision_window(self.created_revision, self.updated_revision)
        member_ids = tuple(member.token_id for member in self.members)
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("iteration members must have unique token IDs")
        for member in self.members:
            if member.settled_revision is not None and not (
                self.created_revision <= member.settled_revision <= self.updated_revision
            ):
                raise ValueError("member settlement revision is outside the frame revision window")

        orders = tuple(item.canonical_order.sort_key() for item in self.continuation_deliveries)
        if orders != tuple(sorted(orders)):
            raise ValueError("continuation deliveries must be in canonical order")
        continuation_ids = tuple(item.token_id for item in self.continuation_deliveries)
        if len(continuation_ids) != len(set(continuation_ids)):
            raise ValueError("a token can deliver only one continuation per iteration frame")
        expected_continuations = {
            member.token_id: member.causal_edge_id
            for member in self.members
            if member.state is IterationMemberState.BACK_EDGE_CONTINUATION
        }
        actual_continuations = {
            item.token_id: item.back_edge_id for item in self.continuation_deliveries
        }
        if actual_continuations != expected_continuations:
            raise ValueError(
                "continuation deliveries must exactly match BACK_EDGE_CONTINUATION members"
            )
        members_by_token = {member.token_id: member for member in self.members}
        if any(
            item.settled_revision != members_by_token[item.token_id].settled_revision
            for item in self.continuation_deliveries
        ):
            raise ValueError(
                "continuation settled_revision must equal its matching member settled_revision"
            )
        if any(
            item.canonical_order.iteration_index != self.iteration_index
            for item in self.continuation_deliveries
        ):
            raise ValueError("continuation ordering must use the frame iteration_index")
        if any(
            not self.created_revision <= item.settled_revision <= self.updated_revision
            for item in self.continuation_deliveries
        ):
            raise ValueError("continuation revision is outside the frame revision window")

        has_active = bool(self.active_member_token_ids)
        if self.state is IterationFrameState.ACTIVE and not has_active:
            raise ValueError("an ACTIVE iteration frame must have an active member")
        if self.state is not IterationFrameState.ACTIVE and has_active:
            raise ValueError("a barrier-ready or settled frame cannot have active members")
        settled = self.state is IterationFrameState.SETTLED
        if settled != (self.settled_revision is not None):
            raise ValueError("settled_revision is required exactly when a frame is SETTLED")
        if self.settled_revision is not None and not (
            self.created_revision <= self.settled_revision <= self.updated_revision
        ):
            raise ValueError("settled_revision is outside the frame revision window")
        return self


class LoopExitRecord(_FrozenContract):
    exit_edge_id: EdgeId
    target_node_id: NodeId
    token_id: TokenId
    outcome: LoopExitResolutionOutcome
    delivery: PayloadDelivery | None = None
    canonical_order: CanonicalTokenOrder
    surviving_fork_lineage: tuple[ForkLineageFrame, ...] | None = None
    crossed_fork_ids: tuple[ForkId, ...] = ()
    settled_revision: StateRevision

    @model_validator(mode="after")
    def _validate_record(self) -> LoopExitRecord:
        if self.canonical_order.token_id != self.token_id:
            raise ValueError("canonical_order token_id must match the loop-exit token")
        delivered = self.outcome is LoopExitResolutionOutcome.DELIVERED
        if delivered != (self.delivery is not None):
            raise ValueError("only a DELIVERED loop-exit record carries a delivery")
        if self.surviving_fork_lineage is None:
            object.__setattr__(
                self,
                "surviving_fork_lineage",
                self.canonical_order.fork_lineage,
            )
        surviving_lineage = self.surviving_fork_lineage or ()
        surviving_ids = tuple(frame.fork_id for frame in surviving_lineage)
        source_lineage = self.canonical_order.fork_lineage
        source_suffix = source_lineage[len(surviving_lineage) :]
        if surviving_lineage != source_lineage[
            : len(surviving_lineage)
        ] or self.crossed_fork_ids != tuple(frame.fork_id for frame in source_suffix):
            raise ValueError(
                "surviving and crossed loop-exit forks must exactly partition source lineage"
            )
        if len(surviving_ids) != len(set(surviving_ids)):
            raise ValueError("surviving loop-exit fork lineage cannot contain duplicates")
        for index, frame in enumerate(surviving_lineage):
            expected_parent = None if index == 0 else surviving_ids[index - 1]
            if frame.parent_fork_id != expected_parent:
                raise ValueError("surviving loop-exit fork lineage contains an orphan parent")
        if len(self.crossed_fork_ids) != len(set(self.crossed_fork_ids)):
            raise ValueError("crossed loop-exit fork identity cannot contain duplicates")
        if set(surviving_ids) & set(self.crossed_fork_ids):
            raise ValueError("surviving and crossed loop-exit forks must be disjoint")
        return self


class LoopExit(_FrozenContract):
    exit_edge_id: EdgeId
    target_node_id: NodeId
    records: tuple[LoopExitRecord, ...] = ()
    resolution_outcome: LoopExitResolutionOutcome | None = None
    resolved_revision: StateRevision | None = None

    @model_validator(mode="after")
    def _validate_exit(self) -> LoopExit:
        if any(item.exit_edge_id != self.exit_edge_id for item in self.records):
            raise ValueError("every loop-exit record must match its exit_edge_id")
        if any(item.target_node_id != self.target_node_id for item in self.records):
            raise ValueError("every loop-exit record must match its target_node_id")
        token_ids = tuple(item.token_id for item in self.records)
        if len(token_ids) != len(set(token_ids)):
            raise ValueError("a loop exit may record a token only once")
        order = tuple(item.canonical_order.sort_key() for item in self.records)
        if order != tuple(sorted(order)):
            raise ValueError("loop-exit records must be frozen in canonical order")
        resolved = self.resolution_outcome is not None
        if resolved != (self.resolved_revision is not None):
            raise ValueError("resolution_outcome and resolved_revision must be recorded together")
        if resolved:
            any_delivered = any(
                item.outcome is LoopExitResolutionOutcome.DELIVERED for item in self.records
            )
            expected = (
                LoopExitResolutionOutcome.DELIVERED
                if any_delivered
                else LoopExitResolutionOutcome.SUPPRESSED
            )
            if self.resolution_outcome is not expected:
                raise ValueError("loop-exit resolution must reflect its accumulated deliveries")
            if self.resolved_revision is not None and any(
                item.settled_revision > self.resolved_revision for item in self.records
            ):
                raise ValueError("resolved_revision cannot precede an exit record")
        return self


class LoopEnclosingOwner(_FrozenContract):
    """The durable outer owner resumed when a loop instance settles."""

    token_id: TokenId
    enclosing_loop_instance_id: LoopInstanceId | None = None
    iteration_frame_id: IterationFrameId | None = None

    @model_validator(mode="after")
    def _validate_nested_identity(self) -> LoopEnclosingOwner:
        if (self.enclosing_loop_instance_id is None) != (self.iteration_frame_id is None):
            raise ValueError(
                "nested enclosing identity requires both enclosing_loop_instance_id "
                "and iteration_frame_id"
            )
        return self


class LoopInstance(_FrozenContract):
    """Durable loop state with bounded, scope-local token allocation metadata.

    ``next_token_ordinal`` is the next never-used ordinal.  A runtime derives a
    deterministic token ID from ``loop_instance_id`` plus that ordinal, then
    advances the cursor atomically.  This standalone snapshot validates the
    exclusive upper bound against currently durable identities; monotonicity
    between revisions is a transition/CAS invariant.
    """

    loop_instance_id: LoopInstanceId
    loop_header_node_id: NodeId
    entry_command_fingerprint: StableId | None = None
    enclosing_owner: LoopEnclosingOwner
    outer_provenance_tag: tuple[ProvenanceFrame, ...] = ()
    frames: tuple[IterationFrame, ...] = ()
    live_child_token_ids: tuple[TokenId, ...]
    next_token_ordinal: CreationOrdinal = Field(
        description=(
            "Next never-used scope-local token ordinal and exclusive upper-bound "
            "cursor for deterministic token ID allocation"
        )
    )
    exits: tuple[LoopExit, ...] = ()
    emitted_continuation_token_ids: tuple[TokenId, ...] = ()
    reducer_fingerprint: StableId | None = None
    reduction_claim_id: StableId | None = None
    reduction_claim_owner_id: StableId | None = None
    reduction_claim_revision: StateRevision | None = None
    reduction_attempt: RetryAttempt = 0
    lifecycle_state: LoopLifecycleState
    created_revision: StateRevision
    updated_revision: StateRevision
    completed_revision: StateRevision | None = None

    @model_validator(mode="after")
    def _validate_loop(self) -> LoopInstance:
        _require_revision_window(self.created_revision, self.updated_revision)
        _require_canonical_provenance(self.outer_provenance_tag, "outer_provenance_tag")
        nested_owner = self.enclosing_owner.enclosing_loop_instance_id is not None
        if bool(self.outer_provenance_tag) != nested_owner:
            if self.outer_provenance_tag:
                raise ValueError("a nested loop requires complete nested enclosing identity")
            raise ValueError("a top-level loop cannot carry nested enclosing identity")
        if self.enclosing_owner.enclosing_loop_instance_id == self.loop_instance_id:
            raise ValueError("a loop instance cannot enclose itself")
        if self.loop_header_node_id in {
            frame.loop_header_node_id for frame in self.outer_provenance_tag
        }:
            raise ValueError("outer_provenance_tag cannot contain this loop's header")
        if any(frame.loop_instance_id != self.loop_instance_id for frame in self.frames):
            raise ValueError("every frame loop_instance_id must match its LoopInstance")
        indices = tuple(frame.iteration_index for frame in self.frames)
        if indices != tuple(sorted(indices)) or len(indices) != len(set(indices)):
            raise ValueError("loop frame indices must be unique and canonical")
        nonsettled = tuple(
            frame for frame in self.frames if frame.state is not IterationFrameState.SETTLED
        )
        if len(nonsettled) > 1 or (nonsettled and nonsettled[0] is not self.frames[-1]):
            raise ValueError("iteration frames cannot overlap")
        for frame in self.frames:
            if not (
                self.created_revision
                <= frame.created_revision
                <= frame.updated_revision
                <= self.updated_revision
            ):
                raise ValueError("frame revisions are outside the loop revision window")

        active_ids = tuple(
            sorted(
                member.token_id
                for frame in self.frames
                for member in frame.members
                if member.state is IterationMemberState.ACTIVE
            )
        )
        if self.live_child_token_ids != tuple(sorted(self.live_child_token_ids)):
            raise ValueError("live_child_token_ids must be unique and sorted")
        if len(self.live_child_token_ids) != len(set(self.live_child_token_ids)):
            raise ValueError("live_child_token_ids must be unique and sorted")
        if self.live_child_token_ids != active_ids:
            raise ValueError("live_child_token_ids must exactly match active frame members")

        exit_ids = tuple(item.exit_edge_id for item in self.exits)
        if exit_ids != tuple(sorted(exit_ids)) or len(exit_ids) != len(set(exit_ids)):
            raise ValueError("loop exits must use unique exit edges in canonical order")
        exit_id_set = set(exit_ids)
        all_members = tuple(member for frame in self.frames for member in frame.members)
        all_member_ids = tuple(member.token_id for member in all_members)
        if len(all_member_ids) != len(set(all_member_ids)):
            raise ValueError("a token cannot belong to more than one retained iteration frame")
        members_by_iteration = {
            frame.iteration_index: {member.token_id: member for member in frame.members}
            for frame in self.frames
        }
        all_records = tuple(record for exit_state in self.exits for record in exit_state.records)
        all_record_ids = tuple(record.token_id for record in all_records)
        if len(all_record_ids) != len(set(all_record_ids)):
            raise ValueError("a token may be recorded only once across loop exits")
        member_iteration_by_id = {
            member.token_id: frame.iteration_index
            for frame in self.frames
            for member in frame.members
        }
        if any(
            record.token_id in member_iteration_by_id
            and member_iteration_by_id[record.token_id] != record.canonical_order.iteration_index
            for record in all_records
        ):
            raise ValueError(
                "an exit record token ID cannot be reused by a different retained iteration"
            )
        emitted_ids = self.emitted_continuation_token_ids
        if emitted_ids != tuple(sorted(emitted_ids)) or len(emitted_ids) != len(set(emitted_ids)):
            raise ValueError("emitted_continuation_token_ids must be unique and sorted")
        retained_ids = set(all_member_ids) | set(all_record_ids)
        if self.enclosing_owner.token_id in retained_ids:
            raise ValueError("a loop-local token ID cannot reuse its enclosing owner token ID")
        if (retained_ids | {self.enclosing_owner.token_id}).intersection(emitted_ids):
            raise ValueError(
                "an emitted continuation token ID must be new within the loop instance"
            )
        durable_loop_local_ids = retained_ids | set(emitted_ids)
        if len(durable_loop_local_ids) > self.next_token_ordinal:
            raise ValueError(
                "next_token_ordinal must be at least the number of durable loop-local token IDs"
            )
        if self.frames:
            for record in all_records:
                iteration = record.canonical_order.iteration_index
                retained_members = members_by_iteration.get(iteration)
                if retained_members is None:
                    if iteration > self.frames[-1].iteration_index:
                        raise ValueError(
                            "loop-exit records cannot refer to a future orphan iteration"
                        )
                    continue
                member = retained_members.get(record.token_id)
                if member is None:
                    raise ValueError("loop-exit records cannot refer to orphan frame members")
                expected_state = (
                    IterationMemberState.EXIT_DELIVERY
                    if record.outcome is LoopExitResolutionOutcome.DELIVERED
                    else IterationMemberState.SUPPRESSED
                )
                if (
                    member.state is not expected_state
                    or member.causal_edge_id != record.exit_edge_id
                ):
                    raise ValueError(
                        "loop-exit records must exactly match their retained frame member outcome"
                    )
                if record.settled_revision != member.settled_revision:
                    raise ValueError(
                        "loop-exit record settled_revision must equal its matching member "
                        "settled_revision"
                    )
            expected_records = {
                (
                    frame.iteration_index,
                    member.token_id,
                    member.causal_edge_id,
                    (
                        LoopExitResolutionOutcome.DELIVERED
                        if member.state is IterationMemberState.EXIT_DELIVERY
                        else LoopExitResolutionOutcome.SUPPRESSED
                    ),
                )
                for frame in self.frames
                for member in frame.members
                if member.state is IterationMemberState.EXIT_DELIVERY
                or (
                    member.state is IterationMemberState.SUPPRESSED
                    and member.causal_edge_id in exit_id_set
                )
            }
            actual_records = {
                (
                    record.canonical_order.iteration_index,
                    record.token_id,
                    record.exit_edge_id,
                    record.outcome,
                )
                for record in all_records
            }
            if not expected_records <= actual_records:
                raise ValueError(
                    "loop-exit records must exactly match retained exit member outcomes"
                )
        for exit_state in self.exits:
            revisions = [item.settled_revision for item in exit_state.records]
            if exit_state.resolved_revision is not None:
                revisions.append(exit_state.resolved_revision)
            if any(
                revision < self.created_revision or revision > self.updated_revision
                for revision in revisions
            ):
                raise ValueError("loop-exit revisions are outside the loop revision window")

        claim_fields = (
            self.reducer_fingerprint,
            self.reduction_claim_id,
            self.reduction_claim_owner_id,
            self.reduction_claim_revision,
        )
        if any(item is not None for item in claim_fields) and not all(
            item is not None for item in claim_fields
        ):
            raise ValueError("a loop reduction claim requires complete reducer ownership")
        if all(item is not None for item in claim_fields):
            if self.lifecycle_state in {
                LoopLifecycleState.CANCELLED,
                LoopLifecycleState.COMPLETED,
            }:
                raise ValueError("a terminal loop cannot retain a reduction claim")
            if not self.frames or self.frames[-1].state is not IterationFrameState.BARRIER_READY:
                raise ValueError("only a barrier-ready loop may retain a reduction claim")
            if self.reduction_attempt < 1:
                raise ValueError("a loop reduction claim requires a positive attempt")
            if self.reduction_claim_revision != self.updated_revision:
                raise ValueError("a loop reduction claim revision must match loop revision")

        completed = self.lifecycle_state in {
            LoopLifecycleState.CANCELLED,
            LoopLifecycleState.COMPLETED,
        }
        if completed != (self.completed_revision is not None):
            raise ValueError("completed_revision is required exactly when a loop is terminal")
        if self.lifecycle_state is LoopLifecycleState.COMPLETED:
            if nonsettled or self.live_child_token_ids:
                raise ValueError("a COMPLETED loop cannot retain a live iteration frame")
            if any(item.resolution_outcome is None for item in self.exits):
                raise ValueError("a COMPLETED loop must resolve every owned exit")
            delivered_exit_count = sum(
                item.resolution_outcome is LoopExitResolutionOutcome.DELIVERED
                for item in self.exits
            )
            if len(emitted_ids) != delivered_exit_count:
                raise ValueError(
                    "a COMPLETED loop must record one emitted continuation per delivered exit"
                )
        elif self.lifecycle_state is LoopLifecycleState.CANCELLED:
            if nonsettled or self.live_child_token_ids:
                raise ValueError("a CANCELLED loop cannot retain a live iteration frame")
            if any(item.resolution_outcome is None for item in self.exits):
                raise ValueError("a CANCELLED loop must resolve every owned exit")
            if emitted_ids:
                raise ValueError("a CANCELLED loop cannot publish exit continuations")
        else:
            if not nonsettled:
                raise ValueError("a RUNNING or STOPPING loop must retain its current frame")
            if any(item.resolution_outcome is not None for item in self.exits):
                raise ValueError("a RUNNING or STOPPING loop cannot resolve exits early")
            if emitted_ids:
                raise ValueError("only a COMPLETED loop may record emitted continuation tokens")
        if self.completed_revision is not None and not (
            self.created_revision <= self.completed_revision <= self.updated_revision
        ):
            raise ValueError("completed_revision is outside the loop revision window")
        return self


class CancellationFence(_FrozenContract):
    """The latest persisted cancellation generation and acknowledgements."""

    generation: CancellationGeneration = 0
    requested_revision: StateRevision | None = None
    acknowledged_token_ids: tuple[TokenId, ...] = ()
    acknowledged_dispatch_ids: tuple[DispatchId, ...] = ()
    state_revision: StateRevision

    @model_validator(mode="after")
    def _validate_fence(self) -> CancellationFence:
        if self.generation == 0:
            if (
                self.requested_revision is not None
                or self.acknowledged_token_ids
                or self.acknowledged_dispatch_ids
            ):
                raise ValueError("generation zero cannot contain cancellation state")
        elif self.requested_revision is None:
            raise ValueError("a positive generation requires requested_revision")
        if self.requested_revision is not None and self.requested_revision > self.state_revision:
            raise ValueError("requested_revision cannot exceed state_revision")
        if self.acknowledged_token_ids != tuple(sorted(self.acknowledged_token_ids)) or len(
            self.acknowledged_token_ids
        ) != len(set(self.acknowledged_token_ids)):
            raise ValueError("acknowledged_token_ids must be unique and sorted")
        if self.acknowledged_dispatch_ids != tuple(
            sorted(self.acknowledged_dispatch_ids)
        ) or len(self.acknowledged_dispatch_ids) != len(set(self.acknowledged_dispatch_ids)):
            raise ValueError("acknowledged_dispatch_ids must be unique and sorted")
        return self


class InFlightDispatch(_FrozenContract):
    """One durable dispatch claim, including replay and cancellation identity."""

    dispatch_id: DispatchId
    idempotency_key: IdempotencyKey
    token: TokenEnvelope
    attempt: RetryAttempt
    cancellation_generation: CancellationGeneration
    lifecycle_state: DispatchLifecycleState
    cancellation_requested_generation: CancellationGeneration | None = None
    cancellation_requested_revision: StateRevision | None = None
    started_revision: StateRevision
    updated_revision: StateRevision

    @model_validator(mode="after")
    def _validate_dispatch(self) -> InFlightDispatch:
        if self.token.scheduling_state is not SchedulingState.EXECUTING:
            raise ValueError("an in-flight dispatch token must be EXECUTING")
        if self.attempt != self.token.retry_attempt:
            raise ValueError("dispatch attempt must match token.retry_attempt")
        if self.cancellation_generation != self.token.cancellation_generation:
            raise ValueError("dispatch cancellation_generation must match the token's generation")
        if self.started_revision < self.token.state_revision:
            raise ValueError("started_revision cannot precede the token state revision")
        if self.updated_revision < self.started_revision:
            raise ValueError("updated_revision cannot precede started_revision")
        request_fields_present = (
            self.cancellation_requested_generation is not None,
            self.cancellation_requested_revision is not None,
        )
        if request_fields_present[0] != request_fields_present[1]:
            raise ValueError(
                "cancellation_requested_generation and revision must be recorded together"
            )
        cancellation_requested = (
            self.lifecycle_state is DispatchLifecycleState.CANCELLATION_REQUESTED
        )
        if cancellation_requested != all(request_fields_present):
            raise ValueError(
                "cancellation request fields are required exactly for CANCELLATION_REQUESTED"
            )
        if cancellation_requested:
            requested_generation = self.cancellation_requested_generation
            requested_revision = self.cancellation_requested_revision
            if requested_generation is None or requested_generation <= self.cancellation_generation:
                raise ValueError(
                    "cancellation_requested_generation must be newer than the dispatch generation"
                )
            if requested_revision is None or not (
                self.started_revision < requested_revision <= self.updated_revision
            ):
                raise ValueError(
                    "cancellation_requested_revision must follow start and not exceed update"
                )
        return self


__all__ = [
    "CancellationFence",
    "CancellationGeneration",
    "CanonicalTokenOrder",
    "CreationOrdinal",
    "DeferredJoinDelivery",
    "DispatchId",
    "DispatchLifecycleState",
    "EdgeId",
    "ForkChild",
    "ForkId",
    "ForkInstance",
    "ForkLifecycleState",
    "ForkLineageFrame",
    "ForkObligation",
    "ForkObligationOutcome",
    "IdempotencyKey",
    "InFlightDispatch",
    "IterationContinuationDelivery",
    "IterationFrame",
    "IterationFrameId",
    "IterationFrameState",
    "IterationIndex",
    "IterationMember",
    "IterationMemberState",
    "IterationMembership",
    "JoinInstance",
    "JoinInstanceId",
    "JoinLifecycleState",
    "JoinObligation",
    "JoinObligationOutcome",
    "LoopExit",
    "LoopEnclosingOwner",
    "LoopExitRecord",
    "LoopExitResolutionOutcome",
    "LoopInstance",
    "LoopInstanceId",
    "LoopLifecycleState",
    "NodeId",
    "ObligationId",
    "PayloadDelivery",
    "ProvenanceFrame",
    "RetryAttempt",
    "SchedulingState",
    "StableId",
    "StateRevision",
    "TokenEnvelope",
    "TokenId",
    "TokenLifecycleState",
]
