"""Persistence-safe contracts for the durable multi-token runtime model."""

from __future__ import annotations

import ast
import inspect

import pytest
from pydantic import ValidationError

from zeroth.contracts.graph.tokens import (
    CancellationFence,
    CanonicalTokenOrder,
    DispatchLifecycleState,
    ForkChild,
    ForkInstance,
    ForkLifecycleState,
    ForkLineageFrame,
    ForkObligation,
    ForkObligationOutcome,
    InFlightDispatch,
    IterationContinuationDelivery,
    IterationFrame,
    IterationFrameState,
    IterationMember,
    IterationMemberState,
    IterationMembership,
    JoinInstance,
    JoinLifecycleState,
    JoinObligation,
    JoinObligationOutcome,
    LoopExit,
    LoopExitRecord,
    LoopExitResolutionOutcome,
    LoopInstance,
    LoopLifecycleState,
    PayloadDelivery,
    ProvenanceFrame,
    SchedulingState,
    TokenEnvelope,
    TokenLifecycleState,
)


def _root_token(**updates: object) -> TokenEnvelope:
    values: dict[str, object] = {
        "token_id": "token-root",
        "current_node_id": "start",
        "payload": {"value": 1, "nested": [True, None]},
        "scheduling_state": SchedulingState.QUEUED,
        "lifecycle_state": TokenLifecycleState.ACTIVE,
        "state_revision": 3,
    }
    values.update(updates)
    return TokenEnvelope(**values)


def _fork_obligation(
    token_id: str,
    ordinal: int,
    *,
    outcome: ForkObligationOutcome | None = None,
    settled_revision: int | None = None,
    join_instance_id: str | None = None,
    exit_edge_id: str | None = None,
) -> ForkObligation:
    return ForkObligation(
        obligation_id=f"fork-obligation-{ordinal}",
        fork_id="fork-1",
        child_token_id=token_id,
        child_ordinal=ordinal,
        outcome=outcome,
        settled_revision=settled_revision,
        join_instance_id=join_instance_id,
        exit_edge_id=exit_edge_id,
    )


def _join_obligation(
    token_id: str,
    ordinal: int,
    *,
    outcome: JoinObligationOutcome | None = None,
    settled_revision: int | None = None,
    delivery: PayloadDelivery | None = None,
) -> JoinObligation:
    return JoinObligation(
        obligation_id=f"join-obligation-{ordinal}",
        join_instance_id="join-1",
        fork_id="fork-1",
        source_token_id=token_id,
        inbound_edge_id=f"edge-{ordinal}",
        child_ordinal=ordinal,
        outcome=outcome,
        settled_revision=settled_revision,
        delivery=delivery,
    )


def _order(token_id: str, ordinal: int, *, iteration: int = 0) -> CanonicalTokenOrder:
    return CanonicalTokenOrder(
        iteration_index=iteration,
        fork_lineage=(
            ForkLineageFrame(
                fork_id="fork-1",
                parent_fork_id=None,
                child_ordinal=ordinal,
            ),
        ),
        child_ordinal=ordinal,
        token_id=token_id,
    )


def test_token_envelope_is_frozen_strict_and_json_round_trips() -> None:
    envelope = _root_token()

    restored = TokenEnvelope.model_validate_json(envelope.model_dump_json())

    assert restored == envelope
    assert restored.model_dump(mode="json")["payload"] == {
        "value": 1,
        "nested": [True, None],
    }
    with pytest.raises(ValidationError, match="frozen"):
        envelope.current_node_id = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        envelope.payload["value"] = 2  # type: ignore[index]
    with pytest.raises(AttributeError):
        envelope.payload["nested"].append(False)  # type: ignore[index, union-attr]
    delivery = PayloadDelivery(payload={"nested": [1]})
    with pytest.raises(TypeError):
        delivery.payload["nested"] = ()  # type: ignore[index]
    assert PayloadDelivery.model_validate_json(delivery.model_dump_json()) == delivery
    with pytest.raises(ValidationError):
        _root_token(scheduling_state="queued")
    with pytest.raises(ValidationError):
        _root_token(payload={"not-json": object()})
    with pytest.raises(ValidationError):
        _root_token(payload={"not-finite": float("nan")})
    with pytest.raises(ValidationError):
        TokenEnvelope(
            token_id="token-root",
            current_node_id="start",
            payload={},
            scheduling_state=SchedulingState.QUEUED,
            lifecycle_state=TokenLifecycleState.ACTIVE,
            state_revision=0,
            unexpected=True,
        )


def test_json_payload_serialization_is_canonical_across_mapping_insertion_order() -> None:
    left_payload = {
        "z": 1,
        "a": {"y": 2, "b": 3},
        "items": [{"z": 4, "a": 5}],
    }
    right_payload = {
        "items": [{"a": 5, "z": 4}],
        "a": {"b": 3, "y": 2},
        "z": 1,
    }

    left_envelope = _root_token(payload=left_payload)
    right_envelope = _root_token(payload=right_payload)
    left_delivery = PayloadDelivery(payload=left_payload)
    right_delivery = PayloadDelivery(payload=right_payload)

    assert left_envelope.model_dump_json() == right_envelope.model_dump_json()
    assert left_delivery.model_dump_json() == right_delivery.model_dump_json()


def test_token_envelope_records_nested_parentage_and_owner_lineage() -> None:
    envelope = _root_token(
        token_id="token-child",
        parent_token_id="token-parent",
        causal_inbound_edge_id="edge-in",
        provenance_tag=(
            ProvenanceFrame(loop_header_node_id="inner", iteration_index=2),
            ProvenanceFrame(loop_header_node_id="outer", iteration_index=5),
        ),
        fork_lineage=(
            ForkLineageFrame(
                fork_id="fork-outer",
                parent_fork_id=None,
                child_ordinal=1,
            ),
            ForkLineageFrame(
                fork_id="fork-inner",
                parent_fork_id="fork-outer",
                child_ordinal=0,
                join_instance_id="join-inner",
            ),
        ),
        iteration_memberships=(
            IterationMembership(
                loop_instance_id="loop-outer",
                parent_loop_instance_id=None,
                iteration_frame_id="frame-outer-5",
                loop_header_node_id="outer",
                iteration_index=5,
            ),
            IterationMembership(
                loop_instance_id="loop-inner",
                parent_loop_instance_id="loop-outer",
                iteration_frame_id="frame-inner-2",
                loop_header_node_id="inner",
                iteration_index=2,
            ),
        ),
    )

    assert envelope.fork_lineage[-1].join_instance_id == "join-inner"
    assert envelope.iteration_memberships[-1].parent_loop_instance_id == "loop-outer"


@pytest.mark.parametrize(
    "updates",
    [
        {
            "parent_token_id": "token-parent",
            "continuation_parent_token_ids": ("token-left", "token-right"),
        },
        {"parent_token_id": "token-root"},
        {"continuation_parent_token_ids": ("token-left", "token-left")},
        {
            "fork_lineage": (
                ForkLineageFrame(
                    fork_id="fork-inner",
                    parent_fork_id="missing-fork",
                    child_ordinal=0,
                ),
            )
        },
        {
            "provenance_tag": (
                ProvenanceFrame(loop_header_node_id="outer", iteration_index=0),
                ProvenanceFrame(loop_header_node_id="inner", iteration_index=0),
            )
        },
        {
            "provenance_tag": (ProvenanceFrame(loop_header_node_id="inner", iteration_index=0),),
            "iteration_memberships": (),
        },
    ],
)
def test_token_envelope_rejects_contradictory_or_orphan_lineage(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _root_token(**updates)


@pytest.mark.parametrize(
    "updates",
    [
        {
            "scheduling_state": SchedulingState.SETTLED,
            "lifecycle_state": TokenLifecycleState.ACTIVE,
        },
        {
            "scheduling_state": SchedulingState.QUEUED,
            "lifecycle_state": TokenLifecycleState.SETTLED,
            "settled_revision": 3,
        },
        {
            "scheduling_state": SchedulingState.EXECUTING,
            "lifecycle_state": TokenLifecycleState.SETTLED,
            "settled_revision": 3,
        },
        {
            "scheduling_state": SchedulingState.QUEUED,
            "lifecycle_state": TokenLifecycleState.CANCELLING,
        },
        {
            "scheduling_state": SchedulingState.SETTLED,
            "lifecycle_state": TokenLifecycleState.SETTLED,
        },
        {
            "scheduling_state": SchedulingState.SETTLED,
            "lifecycle_state": TokenLifecycleState.SETTLED,
            "settled_revision": 4,
        },
    ],
)
def test_token_envelope_rejects_invalid_scheduling_lifecycle_pairs(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _root_token(**updates)


def test_fork_instance_requires_contiguous_ordered_children_and_exact_obligations() -> None:
    fork = ForkInstance(
        fork_id="fork-1",
        parent_token_id="token-root",
        parent_fork_id=None,
        children=(
            ForkChild(token_id="token-a", creation_ordinal=0),
            ForkChild(token_id="token-b", creation_ordinal=1),
        ),
        obligations=(
            _fork_obligation("token-a", 0),
            _fork_obligation("token-b", 1),
        ),
        outstanding_child_count=2,
        lifecycle_state=ForkLifecycleState.OPEN,
        created_revision=4,
        updated_revision=4,
    )

    assert tuple(child.creation_ordinal for child in fork.children) == (0, 1)
    assert ForkInstance.model_validate_json(fork.model_dump_json()) == fork

    with pytest.raises(ValidationError, match="contiguous"):
        ForkInstance.model_validate(
            {
                **fork.model_dump(),
                "children": (
                    ForkChild(token_id="token-a", creation_ordinal=1),
                    ForkChild(token_id="token-b", creation_ordinal=0),
                ),
            }
        )
    with pytest.raises(ValidationError, match="outstanding_child_count"):
        ForkInstance.model_validate({**fork.model_dump(), "outstanding_child_count": 1})
    with pytest.raises(ValidationError, match="exactly match"):
        ForkInstance.model_validate(
            {**fork.model_dump(), "obligations": (_fork_obligation("token-a", 0),)}
        )


def test_fork_obligations_require_explicit_valid_settlement_details() -> None:
    joined = _fork_obligation(
        "token-a",
        0,
        outcome=ForkObligationOutcome.JOINED,
        settled_revision=5,
        join_instance_id="join-1",
    )
    exited = _fork_obligation(
        "token-b",
        1,
        outcome=ForkObligationOutcome.EXITED,
        settled_revision=5,
        exit_edge_id="edge-exit",
    )
    closed = ForkInstance(
        fork_id="fork-1",
        parent_token_id="token-root",
        children=(
            ForkChild(token_id="token-a", creation_ordinal=0),
            ForkChild(token_id="token-b", creation_ordinal=1),
        ),
        obligations=(joined, exited),
        outstanding_child_count=0,
        lifecycle_state=ForkLifecycleState.CLOSED,
        created_revision=4,
        updated_revision=6,
        closed_revision=6,
    )
    assert closed.obligations == (joined, exited)

    with pytest.raises(ValidationError):
        _fork_obligation(
            "token-a",
            0,
            outcome=ForkObligationOutcome.JOINED,
            settled_revision=5,
        )
    with pytest.raises(ValidationError):
        _fork_obligation(
            "token-a",
            0,
            outcome=ForkObligationOutcome.SUPPRESSED,
            settled_revision=5,
            exit_edge_id="not-allowed",
        )
    with pytest.raises(ValidationError):
        _fork_obligation("token-a", 0, settled_revision=5)
    with pytest.raises(ValidationError, match="fork_id"):
        ForkInstance.model_validate(
            {
                **closed.model_dump(),
                "obligations": (
                    joined.model_copy(update={"fork_id": "orphan-fork"}),
                    exited,
                ),
            }
        )


def test_join_instance_readiness_and_closed_continuation_parentage() -> None:
    delivered = _join_obligation(
        "token-a",
        0,
        outcome=JoinObligationOutcome.DELIVERED,
        settled_revision=7,
        delivery=PayloadDelivery(payload={"left": 1}),
    )
    suppressed = _join_obligation(
        "token-b",
        1,
        outcome=JoinObligationOutcome.SUPPRESSED,
        settled_revision=7,
    )
    ready = JoinInstance(
        join_instance_id="join-1",
        fork_id="fork-1",
        target_node_id="join-node",
        obligations=(delivered, suppressed),
        lifecycle_state=JoinLifecycleState.READY,
        created_revision=5,
        updated_revision=7,
    )
    closed = JoinInstance.model_validate(
        {
            **ready.model_dump(),
            "lifecycle_state": JoinLifecycleState.CLOSED,
            "continuation_token_id": "token-continuation",
            "consumed_parent_token_ids": ("token-a", "token-b"),
            "closed_revision": 8,
            "updated_revision": 8,
        }
    )

    assert ready.delivered_obligation_count == 1
    assert closed.consumed_parent_token_ids == ("token-a", "token-b")
    assert JoinInstance.model_validate_json(closed.model_dump_json()) == closed


def test_join_instance_rejects_malformed_obligations_and_lifecycle() -> None:
    delivered = _join_obligation(
        "token-a",
        0,
        outcome=JoinObligationOutcome.DELIVERED,
        settled_revision=7,
        delivery=PayloadDelivery(payload=None),
    )
    pending = _join_obligation("token-b", 1)

    with pytest.raises(ValidationError):
        _join_obligation(
            "token-a",
            0,
            outcome=JoinObligationOutcome.SUPPRESSED,
            settled_revision=7,
            delivery=PayloadDelivery(payload={}),
        )
    with pytest.raises(ValidationError, match="READY"):
        JoinInstance(
            join_instance_id="join-1",
            fork_id="fork-1",
            target_node_id="join-node",
            obligations=(delivered, pending),
            lifecycle_state=JoinLifecycleState.READY,
            created_revision=5,
            updated_revision=7,
        )
    with pytest.raises(ValidationError, match="canonical"):
        JoinInstance(
            join_instance_id="join-1",
            fork_id="fork-1",
            target_node_id="join-node",
            obligations=(pending, delivered),
            lifecycle_state=JoinLifecycleState.OPEN,
            created_revision=5,
            updated_revision=7,
        )
    with pytest.raises(ValidationError, match="join_instance_id"):
        JoinInstance(
            join_instance_id="join-1",
            fork_id="fork-1",
            target_node_id="join-node",
            obligations=(delivered.model_copy(update={"join_instance_id": "orphan-join"}),),
            lifecycle_state=JoinLifecycleState.READY,
            created_revision=5,
            updated_revision=7,
        )


def test_join_instance_rejects_reused_sources_and_self_continuation() -> None:
    delivered = _join_obligation(
        "token-a",
        0,
        outcome=JoinObligationOutcome.DELIVERED,
        settled_revision=7,
        delivery=PayloadDelivery(payload={"value": 1}),
    )
    reused_source = _join_obligation("token-b", 1).model_copy(update={"source_token_id": "token-a"})

    with pytest.raises(ValidationError, match="source_token_id"):
        JoinInstance(
            join_instance_id="join-1",
            fork_id="fork-1",
            target_node_id="join-node",
            obligations=(delivered, reused_source),
            lifecycle_state=JoinLifecycleState.OPEN,
            created_revision=5,
            updated_revision=7,
        )

    suppressed = _join_obligation(
        "token-b",
        1,
        outcome=JoinObligationOutcome.SUPPRESSED,
        settled_revision=7,
    )
    closed_values: dict[str, object] = {
        "join_instance_id": "join-1",
        "fork_id": "fork-1",
        "target_node_id": "join-node",
        "obligations": (delivered, suppressed),
        "lifecycle_state": JoinLifecycleState.CLOSED,
        "continuation_token_id": "token-continuation",
        "consumed_parent_token_ids": ("token-a", "token-b"),
        "created_revision": 5,
        "updated_revision": 8,
        "closed_revision": 8,
    }
    JoinInstance(**closed_values)
    with pytest.raises(ValidationError, match="unique"):
        JoinInstance(
            **{
                **closed_values,
                "consumed_parent_token_ids": ("token-a", "token-a"),
            }
        )
    with pytest.raises(ValidationError, match="continuation"):
        JoinInstance(**{**closed_values, "continuation_token_id": "token-a"})


def test_iteration_frame_tracks_active_members_and_canonical_continuations() -> None:
    active = IterationMember(token_id="token-a", state=IterationMemberState.ACTIVE)
    frame = IterationFrame(
        iteration_frame_id="frame-0",
        loop_instance_id="loop-1",
        iteration_index=0,
        members=(active,),
        state=IterationFrameState.ACTIVE,
        created_revision=2,
        updated_revision=2,
    )
    assert frame.active_member_token_ids == ("token-a",)

    back = IterationMember(
        token_id="token-a",
        state=IterationMemberState.BACK_EDGE_CONTINUATION,
        causal_edge_id="edge-back",
        settled_revision=4,
    )
    ready = IterationFrame(
        iteration_frame_id="frame-0",
        loop_instance_id="loop-1",
        iteration_index=0,
        members=(back,),
        continuation_deliveries=(
            IterationContinuationDelivery(
                token_id="token-a",
                back_edge_id="edge-back",
                delivery=PayloadDelivery(payload={"value": 2}),
                canonical_order=_order("token-a", 0),
                settled_revision=4,
            ),
        ),
        state=IterationFrameState.BARRIER_READY,
        created_revision=2,
        updated_revision=4,
    )
    assert ready.active_member_token_ids == ()
    assert IterationFrame.model_validate_json(ready.model_dump_json()) == ready

    mismatched_delivery = ready.continuation_deliveries[0].model_copy(
        update={"settled_revision": 3}
    )
    with pytest.raises(ValidationError, match="settled_revision must equal"):
        IterationFrame.model_validate(
            {
                **ready.model_dump(),
                "continuation_deliveries": (mismatched_delivery,),
            }
        )

    with pytest.raises(ValidationError, match="ACTIVE"):
        IterationFrame.model_validate({**ready.model_dump(), "state": IterationFrameState.ACTIVE})
    with pytest.raises(ValidationError):
        IterationMember(
            token_id="token-a",
            state=IterationMemberState.ACTIVE,
            settled_revision=4,
        )
    with pytest.raises(ValidationError, match="continuation"):
        IterationFrame.model_validate({**ready.model_dump(), "continuation_deliveries": ()})


def test_loop_exit_records_freeze_in_canonical_order() -> None:
    records = (
        LoopExitRecord(
            exit_edge_id="edge-exit",
            target_node_id="outside",
            token_id="token-a",
            outcome=LoopExitResolutionOutcome.DELIVERED,
            delivery=PayloadDelivery(payload={"value": "a"}),
            canonical_order=_order("token-a", 0, iteration=0),
            settled_revision=5,
        ),
        LoopExitRecord(
            exit_edge_id="edge-exit",
            target_node_id="outside",
            token_id="token-b",
            outcome=LoopExitResolutionOutcome.DELIVERED,
            delivery=PayloadDelivery(payload={"value": "b"}),
            canonical_order=_order("token-b", 1, iteration=1),
            settled_revision=8,
        ),
    )
    exit_state = LoopExit(
        exit_edge_id="edge-exit",
        target_node_id="outside",
        records=records,
        resolution_outcome=LoopExitResolutionOutcome.DELIVERED,
        resolved_revision=9,
    )

    assert tuple(record.token_id for record in exit_state.records) == ("token-a", "token-b")
    assert LoopExit.model_validate_json(exit_state.model_dump_json()) == exit_state
    with pytest.raises(ValidationError, match="canonical"):
        LoopExit(
            exit_edge_id="edge-exit",
            target_node_id="outside",
            records=tuple(reversed(records)),
        )
    with pytest.raises(ValidationError):
        LoopExitRecord.model_validate({**records[0].model_dump(), "token_id": "different"})


def test_loop_instance_validates_frame_ownership_live_members_and_completion() -> None:
    member = IterationMember(token_id="token-a", state=IterationMemberState.ACTIVE)
    frame = IterationFrame(
        iteration_frame_id="frame-0",
        loop_instance_id="loop-1",
        iteration_index=0,
        members=(member,),
        state=IterationFrameState.ACTIVE,
        created_revision=2,
        updated_revision=2,
    )
    running = LoopInstance(
        loop_instance_id="loop-1",
        loop_header_node_id="header",
        enclosing_owner={"token_id": "token-entry"},
        outer_provenance_tag=(),
        frames=(frame,),
        live_child_token_ids=("token-a",),
        owned_token_ids=("token-a", "token-entry"),
        exits=(LoopExit(exit_edge_id="edge-exit", target_node_id="outside"),),
        lifecycle_state=LoopLifecycleState.RUNNING,
        created_revision=2,
        updated_revision=2,
    )
    assert LoopInstance.model_validate_json(running.model_dump_json()) == running

    with pytest.raises(ValidationError, match="live_child_token_ids"):
        LoopInstance.model_validate({**running.model_dump(), "live_child_token_ids": ()})
    with pytest.raises(ValidationError, match="loop_instance_id"):
        LoopInstance.model_validate(
            {
                **running.model_dump(),
                "frames": (frame.model_copy(update={"loop_instance_id": "orphan-loop"}),),
            }
        )
    with pytest.raises(ValidationError, match="outer_provenance_tag"):
        LoopInstance.model_validate(
            {
                **running.model_dump(),
                "outer_provenance_tag": (
                    ProvenanceFrame(loop_header_node_id="header", iteration_index=0),
                ),
            }
        )
    with pytest.raises(ValidationError, match="COMPLETED"):
        LoopInstance.model_validate(
            {
                **running.model_dump(),
                "lifecycle_state": LoopLifecycleState.COMPLETED,
                "completed_revision": 3,
                "updated_revision": 3,
            }
        )


def test_loop_instance_can_restore_only_the_current_nonzero_iteration_frame() -> None:
    frame = IterationFrame(
        iteration_frame_id="frame-5",
        loop_instance_id="loop-1",
        iteration_index=5,
        members=(IterationMember(token_id="token-a", state=IterationMemberState.ACTIVE),),
        state=IterationFrameState.ACTIVE,
        created_revision=12,
        updated_revision=12,
    )

    restored = LoopInstance(
        loop_instance_id="loop-1",
        loop_header_node_id="header",
        enclosing_owner={"token_id": "token-entry"},
        frames=(frame,),
        live_child_token_ids=("token-a",),
        owned_token_ids=("token-a", "token-entry"),
        lifecycle_state=LoopLifecycleState.RUNNING,
        created_revision=2,
        updated_revision=12,
    )

    assert restored.frames[0].iteration_index == 5


def test_completed_loop_instance_has_no_active_iteration_frame() -> None:
    completed = LoopInstance(
        loop_instance_id="loop-1",
        loop_header_node_id="header",
        enclosing_owner={"token_id": "token-entry"},
        frames=(),
        live_child_token_ids=(),
        owned_token_ids=("token-entry",),
        exits=(
            LoopExit(
                exit_edge_id="edge-exit",
                target_node_id="outside",
                resolution_outcome=LoopExitResolutionOutcome.SUPPRESSED,
                resolved_revision=8,
            ),
        ),
        lifecycle_state=LoopLifecycleState.COMPLETED,
        created_revision=2,
        updated_revision=8,
        completed_revision=8,
    )

    assert completed.frames == ()


def test_completed_loop_can_retain_delivered_exits_after_frame_compaction() -> None:
    exit_state = LoopExit(
        exit_edge_id="edge-exit",
        target_node_id="outside",
        records=(
            LoopExitRecord(
                exit_edge_id="edge-exit",
                target_node_id="outside",
                token_id="token-a",
                outcome=LoopExitResolutionOutcome.DELIVERED,
                delivery=PayloadDelivery(payload={"value": 1}),
                canonical_order=_order("token-a", 0),
                settled_revision=7,
            ),
        ),
        resolution_outcome=LoopExitResolutionOutcome.DELIVERED,
        resolved_revision=8,
    )

    completed = LoopInstance(
        loop_instance_id="loop-1",
        loop_header_node_id="header",
        enclosing_owner={"token_id": "token-entry"},
        frames=(),
        live_child_token_ids=(),
        owned_token_ids=("token-a", "token-entry"),
        exits=(exit_state,),
        lifecycle_state=LoopLifecycleState.COMPLETED,
        emitted_continuation_token_ids=("token-continuation",),
        created_revision=2,
        updated_revision=8,
        completed_revision=8,
    )

    assert completed.exits[0].records[0].token_id == "token-a"


def test_running_loop_rejects_early_exit_resolution_and_orphan_records() -> None:
    exited = IterationMember(
        token_id="token-a",
        state=IterationMemberState.EXIT_DELIVERY,
        causal_edge_id="edge-exit",
        settled_revision=6,
    )
    active = IterationMember(token_id="token-b", state=IterationMemberState.ACTIVE)
    frame = IterationFrame(
        iteration_frame_id="frame-0",
        loop_instance_id="loop-1",
        iteration_index=0,
        members=(exited, active),
        state=IterationFrameState.ACTIVE,
        created_revision=2,
        updated_revision=7,
    )
    record = LoopExitRecord(
        exit_edge_id="edge-exit",
        target_node_id="outside",
        token_id="token-a",
        outcome=LoopExitResolutionOutcome.DELIVERED,
        delivery=PayloadDelivery(payload={"value": 1}),
        canonical_order=_order("token-a", 0),
        settled_revision=6,
    )
    pending_exit = LoopExit(
        exit_edge_id="edge-exit",
        target_node_id="outside",
        records=(record,),
    )
    values: dict[str, object] = {
        "loop_instance_id": "loop-1",
        "loop_header_node_id": "header",
        "enclosing_owner": {"token_id": "token-entry"},
        "frames": (frame,),
        "live_child_token_ids": ("token-b",),
        "owned_token_ids": ("token-a", "token-b", "token-entry"),
        "exits": (pending_exit,),
        "lifecycle_state": LoopLifecycleState.RUNNING,
        "created_revision": 2,
        "updated_revision": 7,
    }

    LoopInstance(**values)
    mismatched_record = record.model_copy(update={"settled_revision": 5})
    with pytest.raises(ValidationError, match="settled_revision must equal"):
        LoopInstance(
            **{
                **values,
                "exits": (pending_exit.model_copy(update={"records": (mismatched_record,)}),),
            }
        )
    with pytest.raises(ValidationError, match="cannot resolve exits"):
        LoopInstance(
            **{
                **values,
                "exits": (
                    pending_exit.model_copy(
                        update={
                            "resolution_outcome": LoopExitResolutionOutcome.DELIVERED,
                            "resolved_revision": 7,
                        }
                    ),
                ),
            }
        )
    with pytest.raises(ValidationError, match="orphan"):
        LoopInstance(
            **{
                **values,
                "owned_token_ids": (
                    "token-a",
                    "token-b",
                    "token-entry",
                    "token-orphan",
                ),
                "exits": (
                    LoopExit(
                        exit_edge_id="edge-exit",
                        target_node_id="outside",
                        records=(
                            LoopExitRecord(
                                exit_edge_id="edge-exit",
                                target_node_id="outside",
                                token_id="token-orphan",
                                outcome=LoopExitResolutionOutcome.SUPPRESSED,
                                canonical_order=_order("token-orphan", 0),
                                settled_revision=6,
                            ),
                        ),
                    ),
                ),
            }
        )
    with pytest.raises(ValidationError, match="exactly match"):
        LoopInstance(**{**values, "exits": (pending_exit.model_copy(update={"records": ()}),)})

    suppressed_frame = frame.model_copy(
        update={
            "members": (
                IterationMember(
                    token_id="token-a",
                    state=IterationMemberState.SUPPRESSED,
                    causal_edge_id="edge-exit",
                    settled_revision=6,
                ),
                active,
            )
        }
    )
    with pytest.raises(ValidationError, match="exactly match"):
        LoopInstance(
            **{
                **values,
                "frames": (suppressed_frame,),
                "exits": (pending_exit.model_copy(update={"records": ()}),),
            }
        )


def test_running_loop_allows_exit_records_from_compacted_earlier_frames() -> None:
    current = IterationFrame(
        iteration_frame_id="frame-5",
        loop_instance_id="loop-1",
        iteration_index=5,
        members=(IterationMember(token_id="token-current", state=IterationMemberState.ACTIVE),),
        state=IterationFrameState.ACTIVE,
        created_revision=12,
        updated_revision=12,
    )
    earlier_record = LoopExitRecord(
        exit_edge_id="edge-exit",
        target_node_id="outside",
        token_id="token-earlier",
        outcome=LoopExitResolutionOutcome.DELIVERED,
        delivery=PayloadDelivery(payload={"value": 1}),
        canonical_order=_order("token-earlier", 0, iteration=4),
        settled_revision=10,
    )

    restored = LoopInstance(
        loop_instance_id="loop-1",
        loop_header_node_id="header",
        enclosing_owner={"token_id": "token-entry"},
        frames=(current,),
        live_child_token_ids=("token-current",),
        owned_token_ids=("token-current", "token-earlier", "token-entry"),
        exits=(
            LoopExit(
                exit_edge_id="edge-exit",
                target_node_id="outside",
                records=(earlier_record,),
            ),
        ),
        lifecycle_state=LoopLifecycleState.RUNNING,
        created_revision=2,
        updated_revision=12,
    )

    assert restored.exits[0].records == (earlier_record,)


def test_loop_instance_preserves_enclosing_owner_and_emitted_continuations() -> None:
    active_frame = IterationFrame(
        iteration_frame_id="frame-0",
        loop_instance_id="loop-1",
        iteration_index=0,
        members=(IterationMember(token_id="token-active", state=IterationMemberState.ACTIVE),),
        state=IterationFrameState.ACTIVE,
        created_revision=2,
        updated_revision=2,
    )
    running_values: dict[str, object] = {
        "loop_instance_id": "loop-1",
        "loop_header_node_id": "header",
        "frames": (active_frame,),
        "live_child_token_ids": ("token-active",),
        "lifecycle_state": LoopLifecycleState.RUNNING,
        "created_revision": 2,
        "updated_revision": 2,
    }
    with pytest.raises(ValidationError, match="enclosing_owner"):
        LoopInstance(**running_values, owned_token_ids=("token-active",))

    running = LoopInstance(
        **running_values,
        enclosing_owner={"token_id": "token-entry"},
        owned_token_ids=("token-active", "token-entry"),
    )
    assert running.enclosing_owner.token_id == "token-entry"
    with pytest.raises(ValidationError):
        LoopInstance(
            **running_values,
            enclosing_owner={},
            owned_token_ids=("token-active",),
        )
    with pytest.raises(ValidationError):
        LoopInstance(
            **running_values,
            enclosing_owner={"iteration_frame_id": "outer-frame-2"},
            owned_token_ids=("token-active",),
        )
    nested = LoopInstance(
        **running_values,
        enclosing_owner={
            "token_id": "outer-member-token",
            "iteration_frame_id": "outer-frame-2",
        },
        owned_token_ids=("outer-member-token", "token-active"),
    )
    assert nested.enclosing_owner.token_id == "outer-member-token"
    assert nested.enclosing_owner.iteration_frame_id == "outer-frame-2"
    assert LoopInstance.model_validate_json(nested.model_dump_json()) == nested
    with pytest.raises(ValidationError, match="only a COMPLETED"):
        LoopInstance(
            **running_values,
            enclosing_owner={"token_id": "token-entry"},
            owned_token_ids=("token-active", "token-entry"),
            emitted_continuation_token_ids=("token-continuation",),
        )

    delivered_exit = LoopExit(
        exit_edge_id="edge-exit",
        target_node_id="outside",
        records=(
            LoopExitRecord(
                exit_edge_id="edge-exit",
                target_node_id="outside",
                token_id="token-exited",
                outcome=LoopExitResolutionOutcome.DELIVERED,
                delivery=PayloadDelivery(payload={"value": 1}),
                canonical_order=_order("token-exited", 0),
                settled_revision=7,
            ),
        ),
        resolution_outcome=LoopExitResolutionOutcome.DELIVERED,
        resolved_revision=8,
    )
    completed_values: dict[str, object] = {
        "loop_instance_id": "loop-1",
        "loop_header_node_id": "header",
        "enclosing_owner": {
            "token_id": "outer-member-token",
            "iteration_frame_id": "outer-frame-2",
        },
        "frames": (),
        "live_child_token_ids": (),
        "owned_token_ids": (
            "outer-member-token",
            "token-compacted",
            "token-exited",
        ),
        "exits": (delivered_exit,),
        "lifecycle_state": LoopLifecycleState.COMPLETED,
        "created_revision": 2,
        "updated_revision": 8,
        "completed_revision": 8,
    }
    completed = LoopInstance(
        **completed_values,
        emitted_continuation_token_ids=("token-continuation",),
    )
    assert LoopInstance.model_validate_json(completed.model_dump_json()) == completed
    assert "token-compacted" in completed.owned_token_ids

    with pytest.raises(ValidationError, match="one emitted continuation"):
        LoopInstance(**completed_values)
    with pytest.raises(ValidationError, match="unique and sorted"):
        LoopInstance(
            **completed_values,
            emitted_continuation_token_ids=("token-z", "token-a"),
        )
    with pytest.raises(ValidationError, match="unique and sorted"):
        LoopInstance(
            **completed_values,
            emitted_continuation_token_ids=("token-z", "token-z"),
        )
    with pytest.raises(ValidationError, match="must be new"):
        LoopInstance(
            **completed_values,
            emitted_continuation_token_ids=("token-exited",),
        )
    with pytest.raises(ValidationError, match="must be new"):
        LoopInstance(
            **completed_values,
            emitted_continuation_token_ids=("token-compacted",),
        )

    with pytest.raises(ValidationError, match="owned_token_ids"):
        LoopInstance(
            **{
                **completed_values,
                "owned_token_ids": ("outer-member-token", "token-compacted"),
            },
            emitted_continuation_token_ids=("token-continuation",),
        )
    with pytest.raises(ValidationError, match="unique and sorted"):
        LoopInstance(
            **{
                **completed_values,
                "owned_token_ids": (
                    "token-exited",
                    "token-compacted",
                    "outer-member-token",
                ),
            },
            emitted_continuation_token_ids=("token-continuation",),
        )


def test_loop_continuation_cannot_reuse_a_retained_internal_completion_id() -> None:
    internal = IterationMember(
        token_id="token-internal",
        state=IterationMemberState.INTERNAL_COMPLETION,
        settled_revision=7,
    )
    settled_frame = IterationFrame(
        iteration_frame_id="frame-0",
        loop_instance_id="loop-1",
        iteration_index=0,
        members=(internal,),
        state=IterationFrameState.SETTLED,
        created_revision=2,
        updated_revision=7,
        settled_revision=7,
    )
    delivered_exit = LoopExit(
        exit_edge_id="edge-exit",
        target_node_id="outside",
        records=(
            LoopExitRecord(
                exit_edge_id="edge-exit",
                target_node_id="outside",
                token_id="token-exited",
                outcome=LoopExitResolutionOutcome.DELIVERED,
                delivery=PayloadDelivery(payload={"value": 1}),
                canonical_order=_order("token-exited", 0),
                settled_revision=7,
            ),
        ),
        resolution_outcome=LoopExitResolutionOutcome.DELIVERED,
        resolved_revision=8,
    )

    with pytest.raises(ValidationError, match="must be new"):
        LoopInstance(
            loop_instance_id="loop-1",
            loop_header_node_id="header",
            enclosing_owner={"token_id": "token-entry"},
            frames=(settled_frame,),
            live_child_token_ids=(),
            owned_token_ids=(
                "token-entry",
                "token-exited",
                "token-internal",
            ),
            exits=(delivered_exit,),
            emitted_continuation_token_ids=("token-internal",),
            lifecycle_state=LoopLifecycleState.COMPLETED,
            created_revision=2,
            updated_revision=8,
            completed_revision=8,
        )


def test_cancellation_generation_and_in_flight_dispatch_form_a_monotonic_fence() -> None:
    fence = CancellationFence(
        generation=2,
        requested_revision=8,
        acknowledged_token_ids=("token-a", "token-b"),
        state_revision=9,
    )
    token = _root_token(
        scheduling_state=SchedulingState.EXECUTING,
        cancellation_generation=2,
        retry_attempt=0,
        state_revision=8,
    )
    dispatch = InFlightDispatch(
        dispatch_id="dispatch-token-root",
        idempotency_key="run:token-root:start",
        token=token,
        attempt=0,
        cancellation_generation=2,
        lifecycle_state=DispatchLifecycleState.EXECUTING,
        started_revision=8,
        updated_revision=9,
    )
    retry = InFlightDispatch(
        dispatch_id=dispatch.dispatch_id,
        idempotency_key=dispatch.idempotency_key,
        token=token.model_copy(update={"retry_attempt": 1, "state_revision": 10}),
        attempt=1,
        cancellation_generation=2,
        lifecycle_state=DispatchLifecycleState.EXECUTING,
        started_revision=10,
        updated_revision=10,
    )

    assert CancellationFence.model_validate_json(fence.model_dump_json()) == fence
    assert InFlightDispatch.model_validate_json(dispatch.model_dump_json()) == dispatch
    assert dispatch.dispatch_id == retry.dispatch_id
    assert dispatch.idempotency_key == retry.idempotency_key
    with pytest.raises(ValidationError, match="attempt"):
        InFlightDispatch.model_validate({**dispatch.model_dump(), "attempt": 1})
    with pytest.raises(ValidationError, match="cancellation_generation"):
        InFlightDispatch.model_validate({**dispatch.model_dump(), "cancellation_generation": 1})
    with pytest.raises(ValidationError, match="requested_revision"):
        CancellationFence(generation=1, state_revision=1)
    with pytest.raises(ValidationError, match="sorted"):
        CancellationFence(
            generation=2,
            requested_revision=8,
            acknowledged_token_ids=("token-b", "token-a"),
            state_revision=9,
        )


def test_token_cancellation_state_requires_the_active_positive_generation() -> None:
    with pytest.raises(ValidationError, match="positive cancellation_generation"):
        _root_token(
            scheduling_state=SchedulingState.EXECUTING,
            lifecycle_state=TokenLifecycleState.CANCELLING,
            cancellation_generation=0,
        )
    with pytest.raises(ValidationError, match="positive cancellation_generation"):
        _root_token(
            scheduling_state=SchedulingState.SETTLED,
            lifecycle_state=TokenLifecycleState.SETTLED,
            settled_revision=3,
            cancellation_generation=0,
            cancellation_acknowledged_generation=0,
        )
    with pytest.raises(ValidationError, match="must equal"):
        _root_token(
            scheduling_state=SchedulingState.SETTLED,
            lifecycle_state=TokenLifecycleState.SETTLED,
            settled_revision=3,
            cancellation_generation=2,
            cancellation_acknowledged_generation=1,
        )

    cancelling = _root_token(
        scheduling_state=SchedulingState.EXECUTING,
        lifecycle_state=TokenLifecycleState.CANCELLING,
        cancellation_generation=2,
    )
    cancelled = _root_token(
        scheduling_state=SchedulingState.SETTLED,
        lifecycle_state=TokenLifecycleState.SETTLED,
        settled_revision=3,
        cancellation_generation=2,
        cancellation_acknowledged_generation=2,
    )
    assert cancelling.cancellation_generation == 2
    assert cancelled.cancellation_acknowledged_generation == 2


def test_cancellation_requested_dispatch_preserves_its_older_generation() -> None:
    dispatched_token = _root_token(
        scheduling_state=SchedulingState.EXECUTING,
        cancellation_generation=1,
        state_revision=5,
    )

    dispatch = InFlightDispatch(
        dispatch_id="dispatch-token-root",
        idempotency_key="run:token-root:start",
        token=dispatched_token,
        attempt=0,
        cancellation_generation=1,
        lifecycle_state=DispatchLifecycleState.CANCELLATION_REQUESTED,
        cancellation_requested_generation=2,
        cancellation_requested_revision=8,
        started_revision=5,
        updated_revision=8,
    )

    assert dispatch.cancellation_generation == 1
    assert dispatch.cancellation_requested_generation == 2
    assert InFlightDispatch.model_validate_json(dispatch.model_dump_json()) == dispatch
    with pytest.raises(ValidationError, match="newer"):
        InFlightDispatch.model_validate(
            {**dispatch.model_dump(), "cancellation_requested_generation": 1}
        )
    with pytest.raises(ValidationError, match="recorded together"):
        InFlightDispatch.model_validate(
            {**dispatch.model_dump(), "cancellation_requested_revision": None}
        )


def test_token_contract_module_has_no_runtime_imports() -> None:
    import zeroth.contracts.graph.tokens as tokens

    tree = ast.parse(inspect.getsource(tokens))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert not {name for name in imports if name.startswith("zeroth.runtime")}


def test_graph_package_reexports_the_complete_token_contract_surface() -> None:
    import zeroth.contracts.graph as graph_contracts
    import zeroth.contracts.graph.tokens as tokens

    assert set(tokens.__all__) <= set(graph_contracts.__all__)
    for name in tokens.__all__:
        assert getattr(graph_contracts, name) is getattr(tokens, name)
