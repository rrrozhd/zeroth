"""Snapshot-wide invariants for replayable token-engine state."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zeroth.contracts.graph import (
    CancellationFence,
    DispatchLifecycleState,
    ForkChild,
    ForkInstance,
    ForkLifecycleState,
    ForkLineageFrame,
    ForkObligation,
    ForkObligationOutcome,
    InFlightDispatch,
    JoinInstance,
    JoinLifecycleState,
    JoinObligation,
    JoinObligationOutcome,
    IterationFrame,
    IterationFrameState,
    IterationMember,
    IterationMemberState,
    LoopEnclosingOwner,
    LoopInstance,
    LoopLifecycleState,
    PayloadDelivery,
    SchedulingState,
    TokenEngineSnapshot,
    TokenEngineSnapshotState,
    TokenEnvelope,
    TokenLifecycleState,
)


def _token(token_id: str, state: SchedulingState, *, revision: int = 0) -> TokenEnvelope:
    return TokenEnvelope(
        token_id=token_id,
        current_node_id="node-a",
        payload={"token": token_id},
        lifecycle_state=(
            TokenLifecycleState.SETTLED
            if state is SchedulingState.SETTLED
            else TokenLifecycleState.ACTIVE
        ),
        scheduling_state=state,
        state_revision=revision,
        settled_revision=revision if state is SchedulingState.SETTLED else None,
    )


def _snapshot_data() -> dict[str, object]:
    queued = _token("token-queued", SchedulingState.QUEUED)
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "revision": 0,
        "state": TokenEngineSnapshotState.RUNNING,
        "next_token_ordinal": 1,
        "queue": (queued,),
        "tokens": (queued,),
        "forks": (),
        "joins": (),
        "loops": (),
        "cancellation_fence": CancellationFence(generation=0, state_revision=0),
        "in_flight_dispatches": (),
    }


def _closed_join_snapshot_data() -> dict[str, object]:
    parent = _token("token-parent", SchedulingState.SETTLED)
    source = _token("token-source", SchedulingState.SETTLED).model_copy(
        update={
            "parent_token_id": parent.token_id,
            "fork_lineage": (
                ForkLineageFrame(
                    fork_id="fork-1",
                    child_ordinal=0,
                    join_instance_id="join-1",
                ),
            ),
        }
    )
    fork = ForkInstance(
        fork_id="fork-1",
        parent_token_id=parent.token_id,
        children=(ForkChild(token_id=source.token_id, creation_ordinal=0),),
        obligations=(
            ForkObligation(
                obligation_id="fork-obligation-1",
                fork_id="fork-1",
                child_token_id=source.token_id,
                child_ordinal=0,
                outcome=ForkObligationOutcome.JOINED,
                join_instance_id="join-1",
                settled_revision=0,
            ),
        ),
        outstanding_child_count=0,
        lifecycle_state=ForkLifecycleState.CLOSED,
        created_revision=0,
        updated_revision=0,
        closed_revision=0,
    )
    continuation = _token("token-continuation", SchedulingState.QUEUED).model_copy(
        update={"continuation_parent_token_ids": (source.token_id,)}
    )
    join = JoinInstance(
        join_instance_id="join-1",
        fork_id=fork.fork_id,
        target_node_id="join-node",
        obligations=(
            JoinObligation(
                obligation_id="join-obligation-1",
                join_instance_id="join-1",
                fork_id=fork.fork_id,
                source_token_id=source.token_id,
                inbound_edge_id="edge-a",
                child_ordinal=0,
                outcome=JoinObligationOutcome.DELIVERED,
                delivery=PayloadDelivery(payload={"value": 1}),
                settled_revision=0,
            ),
        ),
        lifecycle_state=JoinLifecycleState.CLOSED,
        continuation_token_id=continuation.token_id,
        consumed_parent_token_ids=(source.token_id,),
        created_revision=0,
        updated_revision=0,
        closed_revision=0,
    )
    data = _snapshot_data()
    data.update(
        queue=(continuation,),
        tokens=(parent, source, continuation),
        forks=(fork,),
        joins=(join,),
        next_token_ordinal=3,
    )
    return data


def test_snapshot_round_trips_exact_structured_state() -> None:
    snapshot = TokenEngineSnapshot.model_validate(_snapshot_data())

    restored = TokenEngineSnapshot.model_validate_json(snapshot.model_dump_json())

    assert restored == snapshot
    assert restored.queue == snapshot.queue


@pytest.mark.parametrize("field", ["queue", "tokens"])
def test_snapshot_rejects_duplicate_token_identity(field: str) -> None:
    data = _snapshot_data()
    data[field] = (*data[field], data[field][0])  # type: ignore[index]

    with pytest.raises(ValidationError, match=r"unique.*IDs"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_queue_token_missing_from_durable_tokens() -> None:
    data = _snapshot_data()
    data["tokens"] = ()

    with pytest.raises(ValidationError, match="queued token"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_token_in_wrong_scheduler_location() -> None:
    data = _snapshot_data()
    data["queue"] = ()

    with pytest.raises(ValidationError, match="exactly one matching scheduler location"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_in_flight_dispatch_with_nonmatching_token_copy() -> None:
    executing = _token("token-executing", SchedulingState.EXECUTING)
    changed = executing.model_copy(update={"payload": {"changed": True}})
    data = _snapshot_data()
    data["next_token_ordinal"] = 2
    data["queue"] = ()
    data["tokens"] = (executing,)
    data["in_flight_dispatches"] = (
        InFlightDispatch(
            dispatch_id="dispatch-1",
            idempotency_key="key-1",
            token=changed,
            attempt=0,
            cancellation_generation=0,
            lifecycle_state=DispatchLifecycleState.EXECUTING,
            started_revision=0,
            updated_revision=0,
        ),
    )

    with pytest.raises(ValidationError, match="exactly match"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_join_waiter_without_matching_unsettled_obligation() -> None:
    waiting = _token("token-waiting", SchedulingState.JOIN_WAITING)
    data = _snapshot_data()
    data["next_token_ordinal"] = 2
    data["queue"] = ()
    data["tokens"] = (waiting,)

    with pytest.raises(ValidationError, match="join-waiting token"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_join_that_references_missing_fork() -> None:
    waiting = _token("token-waiting", SchedulingState.JOIN_WAITING)
    data = _snapshot_data()
    data["next_token_ordinal"] = 2
    data["queue"] = ()
    data["tokens"] = (waiting,)
    data["joins"] = (
        JoinInstance(
            join_instance_id="join-1",
            fork_id="fork-missing",
            target_node_id="join-node",
            obligations=(
                JoinObligation(
                    obligation_id="obligation-1",
                    join_instance_id="join-1",
                    fork_id="fork-missing",
                    source_token_id="token-waiting",
                    inbound_edge_id="edge-a",
                    child_ordinal=0,
                ),
            ),
            lifecycle_state=JoinLifecycleState.OPEN,
            created_revision=0,
            updated_revision=0,
        ),
    )

    with pytest.raises(ValidationError, match="missing fork"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_join_continuation_missing_from_durable_tokens() -> None:
    data = _closed_join_snapshot_data()
    data["queue"] = ()
    data["tokens"] = data["tokens"][:-1]  # type: ignore[index]
    data["joins"] = (
        data["joins"][0].model_copy(update={"continuation_token_id": "token-missing"}),  # type: ignore[index,union-attr]
    )

    with pytest.raises(ValidationError, match="continuation token"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_fork_child_without_matching_token_lineage() -> None:
    data = _closed_join_snapshot_data()
    parent, source, continuation = data["tokens"]  # type: ignore[misc]
    data["tokens"] = (parent, source.model_copy(update={"fork_lineage": ()}), continuation)

    with pytest.raises(ValidationError, match="fork lineage"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_fork_child_without_immediate_parent() -> None:
    data = _closed_join_snapshot_data()
    parent, source, continuation = data["tokens"]  # type: ignore[misc]
    data["tokens"] = (
        parent,
        source.model_copy(update={"parent_token_id": None}),
        continuation,
    )

    with pytest.raises(ValidationError, match="immediate parent"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_fork_child_with_mismatched_immediate_parent() -> None:
    data = _closed_join_snapshot_data()
    parent, source, continuation = data["tokens"]  # type: ignore[misc]
    other = _token("token-other", SchedulingState.SETTLED)
    data["tokens"] = (
        parent,
        source.model_copy(update={"parent_token_id": other.token_id}),
        continuation,
        other,
    )
    data["next_token_ordinal"] = 4

    with pytest.raises(ValidationError, match="immediate parent"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_live_fork_parent_after_child_creation() -> None:
    data = _closed_join_snapshot_data()
    parent, source, continuation = data["tokens"]  # type: ignore[misc]
    live_parent = _token(parent.token_id, SchedulingState.QUEUED)
    data["tokens"] = (live_parent, source, continuation)
    data["queue"] = (live_parent, continuation)

    with pytest.raises(ValidationError, match="settled parent token"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_join_obligation_from_another_fork_cohort() -> None:
    data = _closed_join_snapshot_data()
    join = data["joins"][0]  # type: ignore[index]
    bad_obligation = join.obligations[0].model_copy(update={"child_ordinal": 1})
    data["joins"] = (join.model_copy(update={"obligations": (bad_obligation,)}),)

    with pytest.raises(ValidationError, match="fork cohort"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_join_continuation_with_wrong_parentage() -> None:
    data = _closed_join_snapshot_data()
    parent, source, continuation = data["tokens"]  # type: ignore[misc]
    data["tokens"] = (
        parent,
        source,
        continuation.model_copy(update={"continuation_parent_token_ids": ()}),
    )
    data["queue"] = (data["tokens"][-1],)  # type: ignore[index]

    with pytest.raises(ValidationError, match="continuation parentage"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_duplicate_obligation_id_across_structured_scopes() -> None:
    data = _closed_join_snapshot_data()
    join = data["joins"][0]  # type: ignore[index]
    duplicate = join.obligations[0].model_copy(update={"obligation_id": "fork-obligation-1"})
    data["joins"] = (join.model_copy(update={"obligations": (duplicate,)}),)

    with pytest.raises(ValidationError, match="obligation IDs"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_live_source_for_settled_join_and_fork_obligations() -> None:
    data = _closed_join_snapshot_data()
    parent, source, continuation = data["tokens"]  # type: ignore[misc]
    live_source = source.model_copy(
        update={
            "lifecycle_state": TokenLifecycleState.ACTIVE,
            "scheduling_state": SchedulingState.QUEUED,
            "settled_revision": None,
        }
    )
    data["tokens"] = (parent, live_source, continuation)
    data["queue"] = (live_source, continuation)

    with pytest.raises(ValidationError, match="settled obligation source"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_joined_fork_obligation_without_reverse_join_mapping() -> None:
    data = _closed_join_snapshot_data()
    parent, source, continuation = data["tokens"]  # type: ignore[misc]
    second = source.model_copy(
        update={
            "token_id": "token-source-2",
            "fork_lineage": (
                ForkLineageFrame(
                    fork_id="fork-1",
                    child_ordinal=1,
                    join_instance_id="join-1",
                ),
            ),
        }
    )
    fork = data["forks"][0]  # type: ignore[index]
    expanded = ForkInstance(
        fork_id=fork.fork_id,
        parent_token_id=fork.parent_token_id,
        children=(
            *fork.children,
            ForkChild(token_id=second.token_id, creation_ordinal=1),
        ),
        obligations=(
            *fork.obligations,
            ForkObligation(
                obligation_id="fork-obligation-2",
                fork_id=fork.fork_id,
                child_token_id=second.token_id,
                child_ordinal=1,
                outcome=ForkObligationOutcome.JOINED,
                join_instance_id="join-1",
                settled_revision=0,
            ),
        ),
        outstanding_child_count=0,
        lifecycle_state=ForkLifecycleState.CLOSED,
        created_revision=0,
        updated_revision=0,
        closed_revision=0,
    )
    data["tokens"] = (parent, source, second, continuation)
    data["forks"] = (expanded,)
    data["next_token_ordinal"] = 4

    with pytest.raises(ValidationError, match="bijection"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_continuation_parent_cycle() -> None:
    first = _token("token-a", SchedulingState.SETTLED).model_copy(
        update={"continuation_parent_token_ids": ("token-b",)}
    )
    second = _token("token-b", SchedulingState.SETTLED).model_copy(
        update={"continuation_parent_token_ids": ("token-a",)}
    )
    data = _snapshot_data()
    data.update(queue=(), tokens=(first, second), next_token_ordinal=2)

    with pytest.raises(ValidationError, match="parent ownership contains a cycle"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_frame_member_without_matching_token_membership() -> None:
    owner = _token("token-owner", SchedulingState.SETTLED)
    child = _token("token-child", SchedulingState.QUEUED)
    frame = IterationFrame(
        iteration_frame_id="frame-1",
        loop_instance_id="loop-1",
        iteration_index=0,
        members=(IterationMember(token_id=child.token_id, state=IterationMemberState.ACTIVE),),
        state=IterationFrameState.ACTIVE,
        created_revision=0,
        updated_revision=0,
    )
    loop = LoopInstance(
        loop_instance_id="loop-1",
        loop_header_node_id="loop-header",
        enclosing_owner=LoopEnclosingOwner(token_id=owner.token_id),
        frames=(frame,),
        live_child_token_ids=(child.token_id,),
        next_token_ordinal=1,
        lifecycle_state=LoopLifecycleState.RUNNING,
        created_revision=0,
        updated_revision=0,
    )
    data = _snapshot_data()
    data.update(
        queue=(child,),
        tokens=(owner, child),
        loops=(loop,),
        next_token_ordinal=2,
    )

    with pytest.raises(ValidationError, match="matching iteration membership"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_orphan_parent_token() -> None:
    child = _token("token-child", SchedulingState.QUEUED).model_copy(
        update={"parent_token_id": "token-missing"}
    )
    data = _snapshot_data()
    data["queue"] = (child,)
    data["tokens"] = (child,)

    with pytest.raises(ValidationError, match="parent token"):
        TokenEngineSnapshot.model_validate(data)


def test_terminal_snapshot_must_be_empty_but_retains_cancellation_fence() -> None:
    data = _snapshot_data()
    data["state"] = TokenEngineSnapshotState.CANCELLED
    data["cancellation_fence"] = CancellationFence(
        generation=1,
        requested_revision=0,
        state_revision=0,
    )

    with pytest.raises(ValidationError, match="terminal snapshot"):
        TokenEngineSnapshot.model_validate(data)

    data["queue"] = ()
    data["tokens"] = ()
    terminal = TokenEngineSnapshot.model_validate(data)
    assert terminal.cancellation_fence is not None


def test_cancelled_snapshot_requires_positive_cancellation_generation() -> None:
    data = _snapshot_data()
    data.update(
        state=TokenEngineSnapshotState.CANCELLED,
        queue=(),
        tokens=(),
    )

    with pytest.raises(ValidationError, match="positive cancellation fence"):
        TokenEngineSnapshot.model_validate(data)


def test_cancelled_snapshot_retains_ack_history_after_token_compaction() -> None:
    data = _snapshot_data()
    data.update(
        state=TokenEngineSnapshotState.CANCELLED,
        queue=(),
        tokens=(),
        cancellation_fence=CancellationFence(
            generation=2,
            requested_revision=0,
            acknowledged_token_ids=("compacted-token",),
            state_revision=0,
        ),
    )

    acknowledged = TokenEngineSnapshot.model_validate(data)
    assert acknowledged.cancellation_fence.acknowledged_token_ids == ("compacted-token",)

    data["cancellation_fence"] = CancellationFence(
        generation=2,
        requested_revision=0,
        acknowledged_token_ids=(),
        state_revision=0,
    )
    revision_fenced = TokenEngineSnapshot.model_validate(data)
    assert revision_fenced.cancellation_fence.acknowledged_token_ids == ()


def test_snapshot_rejects_component_revision_from_the_future() -> None:
    data = _snapshot_data()
    future = _token("token-queued", SchedulingState.QUEUED, revision=1)
    data["queue"] = (future,)
    data["tokens"] = (future,)

    with pytest.raises(ValidationError, match="snapshot revision"):
        TokenEngineSnapshot.model_validate(data)


def test_snapshot_rejects_unknown_schema_version() -> None:
    data = _snapshot_data()
    data["schema_version"] = 2

    with pytest.raises(ValidationError, match="schema_version"):
        TokenEngineSnapshot.model_validate(data)
