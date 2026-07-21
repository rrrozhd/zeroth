"""Canonical join input preparation and reducer adaptation."""

from __future__ import annotations

import hashlib
import json
from typing import cast

from pydantic import JsonValue

from zeroth.contracts.graph.models import JoinConfig
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshot
from zeroth.contracts.graph.tokens import (
    CanonicalTokenOrder,
    JoinInstance,
    JoinObligationOutcome,
)
from zeroth.runtime.orchestration.token_join_models import (
    JoinReducerInput,
    TokenJoinTransitionError,
)
from zeroth.runtime.parallel.reducers import dispatch_strategy


def _join_inputs(snapshot: TokenEngineSnapshot, join: JoinInstance) -> tuple[JoinReducerInput, ...]:
    tokens = {token.token_id: token for token in snapshot.tokens}
    inputs: list[JoinReducerInput] = []
    for obligation in join.obligations:
        if obligation.outcome is not JoinObligationOutcome.DELIVERED:
            continue
        source = tokens[obligation.source_token_id]
        iteration_index = source.provenance_tag[-1].iteration_index if source.provenance_tag else 0
        assert obligation.delivery is not None
        inputs.append(
            JoinReducerInput(
                source_token_id=source.token_id,
                inbound_edge_id=obligation.inbound_edge_id,
                payload=obligation.delivery.model_dump(mode="json")["payload"],
                order=CanonicalTokenOrder(
                    iteration_index=iteration_index,
                    fork_lineage=source.fork_lineage,
                    child_ordinal=obligation.child_ordinal,
                    token_id=source.token_id,
                ),
            )
        )
    return tuple(sorted(inputs, key=lambda item: item.order.sort_key()))


def _at_path(value: JsonValue, path: str | None) -> JsonValue:
    if path is None:
        return value
    result: JsonValue = value
    for part in reversed(tuple(item for item in path.split(".") if item)):
        result = cast(JsonValue, {part: result})
    return result


def reduce_join_inputs(config: JoinConfig, inputs: tuple[JoinReducerInput, ...]) -> JsonValue:
    """Adapt labelled engine inputs to the established ``JoinConfig`` contract.

    ``JoinReducer`` implementations receive the full canonical labelled tuple.
    Legacy/configured reducers are two-argument payload folds, so this explicit
    compatibility boundary unwraps payloads without changing their order.
    """
    payloads = [item.payload for item in inputs]
    reduced = dispatch_strategy(
        config.merge_strategy,
        cast(list[dict[str, object] | None], payloads),
        reducer_ref=config.reducer_ref,
    )
    return _at_path(cast(JsonValue, reduced), config.merge_path)


def _json_value(value: object) -> JsonValue:
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        return cast(JsonValue, json.loads(encoded))
    except (TypeError, ValueError) as exc:
        raise TokenJoinTransitionError("join reducer output must be JSON serializable") from exc


def _config_fingerprint(config: JoinConfig) -> str:
    encoded = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


__all__ = ["reduce_join_inputs"]
