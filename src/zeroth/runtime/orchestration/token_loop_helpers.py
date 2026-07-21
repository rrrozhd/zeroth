"""Identity, immutable replacement, and canonical-order helpers for loops."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from pydantic import BaseModel, JsonValue

from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    CanonicalTokenOrder,
    ForkLineageFrame,
    IterationMembership,
    LoopInstance,
    PayloadDelivery,
    ProvenanceFrame,
    SchedulingState,
    TokenEnvelope,
)
from zeroth.runtime.orchestration.token_loop_models import TokenLoopTransitionError
from zeroth.runtime.orchestration.token_scheduler import (
    TokenSchedulerTransitionError,
    _matching_dispatch,
    _stable_id,
)


def model_data(model: BaseModel) -> dict[str, object]:
    data = {name: getattr(model, name) for name in type(model).model_fields}
    if "payload" in data:
        data["payload"] = model.model_dump(mode="json")["payload"]
    return data


def next_snapshot(snapshot: TokenEngineSnapshot, **updates: object) -> TokenEngineSnapshot:
    data = model_data(snapshot)
    data.update(updates)
    data["revision"] = snapshot.revision + 1
    return TokenEngineSnapshot.model_validate(data)


def updated_token(token: TokenEnvelope, **updates: object) -> TokenEnvelope:
    data = model_data(token)
    data.update(updates)
    return TokenEnvelope.model_validate(data)


def replace_token(
    tokens: tuple[TokenEnvelope, ...], replacement: TokenEnvelope
) -> tuple[TokenEnvelope, ...]:
    return tuple(replacement if item.token_id == replacement.token_id else item for item in tokens)


def replace_loop(
    loops: tuple[LoopInstance, ...], replacement: LoopInstance
) -> tuple[LoopInstance, ...]:
    return tuple(
        replacement if item.loop_instance_id == replacement.loop_instance_id else item
        for item in loops
    )


def stable_fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def entry_fingerprint(
    *,
    token_id: str,
    loop_header_node_id: str,
    body_node_id: str,
    inbound_edge_id: str,
    exit_routes: Mapping[str, str],
    dispatch_id: str | None = None,
    attempt: int | None = None,
    cancellation_generation: int | None = None,
) -> str:
    return stable_fingerprint(
        {
            "token_id": token_id,
            "loop_header_node_id": loop_header_node_id,
            "body_node_id": body_node_id,
            "inbound_edge_id": inbound_edge_id,
            "exit_routes": sorted(exit_routes.items()),
            "dispatch_id": dispatch_id,
            "attempt": attempt,
            "cancellation_generation": cancellation_generation,
        }
    )


def loop_id(snapshot: TokenEngineSnapshot, owner: TokenEnvelope, header: str) -> str:
    material = {
        "owner": owner.token_id,
        "header": header,
        "outer_provenance": [item.model_dump(mode="json") for item in owner.provenance_tag],
    }
    ordinal = int(stable_fingerprint(material)[:12], 16)
    return _stable_id("loop", snapshot.run_id, owner.token_id, ordinal)


def loop_token_id(snapshot: TokenEngineSnapshot, loop_instance_id: str, ordinal: int) -> str:
    return _stable_id("tok", snapshot.run_id, f"loop:{loop_instance_id}", ordinal)


def frame_id(snapshot: TokenEngineSnapshot, loop_instance_id: str, index: int) -> str:
    return _stable_id("itr", snapshot.run_id, loop_instance_id, index)


def source_token(
    snapshot: TokenEngineSnapshot,
    token_id: str,
    *,
    dispatch_id: str | None = None,
    attempt: int | None = None,
    cancellation_generation: int | None = None,
) -> TokenEnvelope:
    token = next((item for item in snapshot.tokens if item.token_id == token_id), None)
    if token is None:
        raise TokenLoopTransitionError(f"loop source token {token_id!r} does not exist")
    if dispatch_id is not None:
        if attempt is None or cancellation_generation is None:
            raise TokenLoopTransitionError("dispatch commands require attempt and generation")
        try:
            dispatch = _matching_dispatch(
                snapshot,
                dispatch_id=dispatch_id,
                attempt=attempt,
                cancellation_generation=cancellation_generation,
            )
        except TokenSchedulerTransitionError as exc:
            raise TokenLoopTransitionError(str(exc)) from exc
        if dispatch.token.token_id != token_id:
            raise TokenLoopTransitionError("dispatch token identity contradicts token_id")
        return dispatch.token
    if attempt is not None or cancellation_generation is not None:
        raise TokenLoopTransitionError("attempt and generation require dispatch_id")
    if token.scheduling_state is not SchedulingState.QUEUED:
        raise TokenLoopTransitionError("direct loop commands require a queued token")
    return token


def canonical_order(token: TokenEnvelope) -> CanonicalTokenOrder:
    child_ordinal = token.fork_lineage[-1].child_ordinal if token.fork_lineage else 0
    iteration_index = token.provenance_tag[-1].iteration_index if token.provenance_tag else 0
    return CanonicalTokenOrder(
        iteration_index=iteration_index,
        fork_lineage=token.fork_lineage,
        child_ordinal=child_ordinal,
        token_id=token.token_id,
    )


def delivery(value: JsonValue) -> PayloadDelivery:
    return PayloadDelivery(payload=value)


def outer_memberships(owner: TokenEnvelope) -> tuple[IterationMembership, ...]:
    return owner.iteration_memberships


def next_provenance(loop: LoopInstance, index: int) -> tuple[ProvenanceFrame, ...]:
    return tuple(
        sorted(
            (
                *loop.outer_provenance_tag,
                ProvenanceFrame(
                    loop_header_node_id=loop.loop_header_node_id, iteration_index=index
                ),
            ),
            key=lambda item: item.loop_header_node_id,
        )
    )


def common_fork_lineage(tokens: tuple[TokenEnvelope, ...]) -> tuple[ForkLineageFrame, ...]:
    if not tokens:
        return ()
    prefix = list(tokens[0].fork_lineage)
    for token in tokens[1:]:
        prefix = [
            frame
            for index, frame in enumerate(prefix)
            if index < len(token.fork_lineage) and token.fork_lineage[index] == frame
        ]
        while prefix and tuple(prefix) != token.fork_lineage[: len(prefix)]:
            prefix.pop()
    return tuple(prefix)


__all__ = [
    "canonical_order",
    "common_fork_lineage",
    "delivery",
    "entry_fingerprint",
    "frame_id",
    "loop_id",
    "loop_token_id",
    "model_data",
    "next_provenance",
    "next_snapshot",
    "outer_memberships",
    "replace_loop",
    "replace_token",
    "source_token",
    "stable_fingerprint",
    "updated_token",
]
