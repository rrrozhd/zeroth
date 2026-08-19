"""Content-free token usage observations captured from LangGraph callbacks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

_DETAIL_FIELDS = ("input_token_details", "output_token_details")


def _freeze(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class UsageObservation:
    """Raw provider usage plus whether cache/reasoning attribution is supportable."""

    run_id: str
    raw_usage: Mapping[str, Any]
    cost_attribution_complete: bool
    missing_fields: tuple[str, ...]

    @classmethod
    def from_usage(cls, run_id: str, usage: object) -> UsageObservation | None:
        """Copy JSON-shaped usage without retaining message or provider objects."""
        if type(usage) is not dict:
            return None
        try:
            copied = json.loads(json.dumps(usage, sort_keys=True, allow_nan=False))
        except (TypeError, ValueError):
            return None
        missing = tuple(
            field for field in _DETAIL_FIELDS if type(copied.get(field)) is not dict
        )
        return cls(
            run_id=str(run_id),
            raw_usage=_freeze(copied),
            cost_attribution_complete=not missing,
            missing_fields=missing,
        )


__all__ = ["UsageObservation"]
