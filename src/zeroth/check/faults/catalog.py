"""Closed V1 fault catalog with a single optional add-on."""

from __future__ import annotations

from zeroth.check.faults.models import MANDATORY_FAULTS, FaultName

OPTIONAL_FAULTS = frozenset({FaultName.ERROR_BEFORE_EFFECT})


def validate_additional(names: list[str]) -> tuple[FaultName, ...]:
    selected: list[FaultName] = []
    for name in names:
        try:
            parsed = FaultName(name)
        except ValueError as exc:
            raise ValueError(f"unknown optional fault: {name}") from exc
        if parsed not in OPTIONAL_FAULTS:
            raise ValueError(f"fault is not an optional add-on: {name}")
        selected.append(parsed)
    if len(selected) != len(set(selected)):
        raise ValueError("optional fault names must be unique")
    return tuple(selected)


__all__ = ["MANDATORY_FAULTS", "OPTIONAL_FAULTS", "validate_additional"]
