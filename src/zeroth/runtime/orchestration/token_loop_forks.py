"""Durable fork ownership changes caused by loop settlement and exits."""

from __future__ import annotations

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    ForkChild,
    ForkInstance,
    ForkLifecycleState,
    ForkLineageFrame,
    ForkObligation,
    ForkObligationOutcome,
    LoopExit,
    LoopExitResolutionOutcome,
    LoopInstance,
    SchedulingState,
    TokenEnvelope,
)
from zeroth.runtime.orchestration.token_loop_helpers import model_data, updated_token
from zeroth.runtime.orchestration.token_loop_models import TokenLoopTransitionError
from zeroth.runtime.orchestration.token_scheduler import _stable_id


def _settle_crossed_exit_forks(
    snapshot: TokenEngineSnapshot,
    token_id: str,
    crossed_fork_ids: tuple[str, ...],
    *,
    edge_id: str,
    revision: int,
) -> tuple[ForkInstance, ...]:
    if not crossed_fork_ids:
        return snapshot.forks
    token = next(item for item in snapshot.tokens if item.token_id == token_id)
    frames = {frame.fork_id: frame for frame in token.fork_lineage}
    forks = {fork.fork_id: fork for fork in snapshot.forks}
    for fork_id in reversed(crossed_fork_ids):
        frame = frames.get(fork_id)
        fork = forks.get(fork_id)
        if frame is None or fork is None:
            raise TokenLoopTransitionError("crossed fork has no durable lineage owner")
        obligations: list[ForkObligation] = []
        matched = False
        for obligation in fork.obligations:
            if obligation.child_ordinal != frame.child_ordinal:
                obligations.append(obligation)
                continue
            if obligation.outcome is not None:
                raise TokenLoopTransitionError("crossed fork obligation is already settled")
            matched = True
            obligations.append(
                ForkObligation.model_validate(
                    {
                        **model_data(obligation),
                        "outcome": ForkObligationOutcome.EXITED,
                        "exit_edge_id": edge_id,
                        "settled_revision": revision,
                    }
                )
            )
        if not matched:
            raise TokenLoopTransitionError("crossed fork has no matching obligation slot")
        outstanding = sum(item.outcome is None for item in obligations)
        forks[fork_id] = ForkInstance.model_validate(
            {
                **model_data(fork),
                "obligations": tuple(obligations),
                "outstanding_child_count": outstanding,
                "lifecycle_state": (
                    ForkLifecycleState.OPEN if outstanding else ForkLifecycleState.CLOSED
                ),
                "updated_revision": revision,
                "closed_revision": None if outstanding else revision,
            }
        )
    return tuple(forks[item.fork_id] for item in snapshot.forks)


def _exit_fork_ownership(
    snapshot: TokenEngineSnapshot,
    token_id: str,
    outermost_loop_id: str,
    crossed_fork_ids: tuple[str, ...] | None,
) -> tuple[tuple[ForkLineageFrame, ...], tuple[str, ...]]:
    token = next(item for item in snapshot.tokens if item.token_id == token_id)
    loop = next(item for item in snapshot.loops if item.loop_instance_id == outermost_loop_id)
    owner = next(item for item in snapshot.tokens if item.token_id == loop.enclosing_owner.token_id)
    owner_lineage = owner.fork_lineage
    if token.fork_lineage[: len(owner_lineage)] != owner_lineage:
        raise TokenLoopTransitionError("loop exit fork lineage contradicts its enclosing owner")
    lineage_ids = tuple(frame.fork_id for frame in token.fork_lineage)
    automatic = lineage_ids[len(owner_lineage) :]
    resolved = automatic if crossed_fork_ids is None else tuple(crossed_fork_ids)
    if resolved and lineage_ids[-len(resolved) :] != resolved:
        raise TokenLoopTransitionError("crossed forks must be one canonical innermost suffix")
    if automatic and (len(resolved) < len(automatic) or resolved[-len(automatic) :] != automatic):
        raise TokenLoopTransitionError("loop-internal forks must be crossed by every loop exit")
    surviving = (
        token.fork_lineage[: len(token.fork_lineage) - len(resolved)]
        if resolved
        else token.fork_lineage
    )
    return surviving, resolved


def _exit_lineage(exit_state: LoopExit) -> tuple[ForkLineageFrame, ...]:
    delivered = tuple(
        item for item in exit_state.records if item.outcome is LoopExitResolutionOutcome.DELIVERED
    )
    if not delivered:
        return ()
    lineage = delivered[0].surviving_fork_lineage
    if any(item.surviving_fork_lineage != lineage for item in delivered[1:]):
        raise TokenLoopTransitionError(
            "loop exit deliveries disagree on durable surviving fork ownership"
        )
    return lineage


def _transfer_exit_fork_slot(
    forks: tuple[ForkInstance, ...],
    continuation: TokenEnvelope,
    revision: int,
) -> tuple[ForkInstance, ...]:
    if not continuation.fork_lineage:
        return forks
    frame = continuation.fork_lineage[-1]
    owner = next((item for item in forks if item.fork_id == frame.fork_id), None)
    if owner is None:
        raise TokenLoopTransitionError("loop exit continuation has a missing fork owner")
    if owner.lifecycle_state is ForkLifecycleState.CLOSED:
        raise TokenLoopTransitionError("loop exit cannot resume a closed fork obligation")
    children: list[ForkChild] = []
    obligations: list[ForkObligation] = []
    matched = False
    for child in owner.children:
        if child.creation_ordinal == frame.child_ordinal:
            matched = True
            children.append(
                ForkChild(
                    token_id=continuation.token_id,
                    creation_ordinal=child.creation_ordinal,
                )
            )
        else:
            children.append(child)
    for obligation in owner.obligations:
        if obligation.child_ordinal != frame.child_ordinal:
            obligations.append(obligation)
            continue
        if obligation.outcome is not None:
            raise TokenLoopTransitionError("loop exit fork obligation was already settled")
        obligations.append(
            ForkObligation.model_validate(
                {**model_data(obligation), "child_token_id": continuation.token_id}
            )
        )
    if not matched:
        raise TokenLoopTransitionError("loop exit has no fork child slot to resume")
    replacement = ForkInstance.model_validate(
        {
            **model_data(owner),
            "children": tuple(children),
            "obligations": tuple(obligations),
            "updated_revision": revision,
        }
    )
    return tuple(replacement if item.fork_id == owner.fork_id else item for item in forks)


def _apply_exit_fork_ownership(
    snapshot: TokenEngineSnapshot,
    loop: LoopInstance,
    continuations: tuple[TokenEnvelope, ...],
    revision: int,
) -> tuple[tuple[TokenEnvelope, ...], tuple[ForkInstance, ...]]:
    groups: dict[tuple[ForkLineageFrame, ...], list[TokenEnvelope]] = {}
    for continuation in continuations:
        if continuation.fork_lineage:
            groups.setdefault(continuation.fork_lineage, []).append(continuation)
    forks = snapshot.forks
    replacements = {item.token_id: item for item in continuations}
    for lineage, group in groups.items():
        if len(group) == 1:
            forks = _transfer_exit_fork_slot(forks, group[0], revision)
            continue
        outer_frame = lineage[-1]
        outer = next((item for item in forks if item.fork_id == outer_frame.fork_id), None)
        if outer is None or outer.lifecycle_state is ForkLifecycleState.CLOSED:
            raise TokenLoopTransitionError("loop exit fan-out has no open outer fork owner")
        parent = next(
            (
                child
                for child in outer.children
                if child.creation_ordinal == outer_frame.child_ordinal
            ),
            None,
        )
        obligation = next(
            (item for item in outer.obligations if item.child_ordinal == outer_frame.child_ordinal),
            None,
        )
        if parent is None or obligation is None or obligation.outcome is not None:
            raise TokenLoopTransitionError("loop exit fan-out outer slot is not resumable")
        fork_id = _stable_id(
            "fork",
            snapshot.run_id,
            f"loop-exit:{loop.loop_instance_id}:{parent.token_id}",
            0,
        )
        if any(item.fork_id == fork_id for item in forks):
            raise TokenLoopTransitionError("loop exit fan-out identity already exists")
        children: list[ForkChild] = []
        obligations: list[ForkObligation] = []
        for ordinal, continuation in enumerate(group):
            resumed = updated_token(
                continuation,
                fork_lineage=(
                    *lineage,
                    ForkLineageFrame(
                        fork_id=fork_id,
                        parent_fork_id=outer.fork_id,
                        child_ordinal=ordinal,
                    ),
                ),
                scheduling_state=SchedulingState.QUEUED,
                state_revision=revision,
            )
            replacements[resumed.token_id] = resumed
            children.append(ForkChild(token_id=resumed.token_id, creation_ordinal=ordinal))
            obligations.append(
                ForkObligation(
                    obligation_id=_stable_id("obl", snapshot.run_id, fork_id, ordinal),
                    fork_id=fork_id,
                    child_token_id=resumed.token_id,
                    child_ordinal=ordinal,
                )
            )
        forks = (
            *forks,
            ForkInstance(
                fork_id=fork_id,
                parent_token_id=parent.token_id,
                parent_fork_id=outer.fork_id,
                children=tuple(children),
                obligations=tuple(obligations),
                outstanding_child_count=len(children),
                lifecycle_state=ForkLifecycleState.OPEN,
                created_revision=revision,
                updated_revision=revision,
            ),
        )
    return tuple(replacements[item.token_id] for item in continuations), forks


__all__: list[str] = []
