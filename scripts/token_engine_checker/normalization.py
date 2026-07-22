"""Public-contract trace normalization."""

from __future__ import annotations

import json
from collections.abc import Mapping

from .oracle import Trace


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _persisted(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    normalized = dict(value)
    terminal = normalized.get("terminal")
    if isinstance(terminal, list):
        normalized["terminal"] = sorted(terminal, key=_json)
    return normalized


def normalize_trace(trace: Trace) -> dict[str, object]:
    """Remove only ordering declared irrelevant by the trace contract."""
    resolutions = sorted(
        (
            event.edge_id,
            event.token_id,
            event.source,
            event.target,
            event.delivered,
            _json(event.payload),
        )
        for event in trace.resolutions
    )
    dispatches = sorted(
        (
            event.node_id,
            event.token_id,
            event.attempt,
            event.inbound_edge_id,
            _json(event.payload),
        )
        for event in trace.dispatches
    )
    return {
        "resolutions": resolutions,
        "dispatches": dispatches,
        "terminal_output": _json(trace.terminal_output),
        "pending": sorted(trace.pending),
        "lifecycle": sorted(trace.lifecycle),
        "persisted_state": _json(_persisted(trace.persisted_state)),
    }
