"""Canonical effective-mode semantics for the structured token engine."""

from __future__ import annotations

from typing import Literal

from zeroth.contracts.graph.models import ExecutionSettings

EngineMode = Literal["legacy", "token"]


def token_engine_enabled(settings: ExecutionSettings) -> bool:
    """Return the effective mode while preserving the pinned raw field ABI.

    An unauthored flag selects the structured engine. Explicit ``True`` also
    selects it; explicit ``False`` is the temporary legacy escape hatch.
    """
    return (
        "sequential_join_enabled" not in settings.model_fields_set
        or settings.sequential_join_enabled is True
    )


def effective_engine_mode(settings: ExecutionSettings) -> EngineMode:
    return "token" if token_engine_enabled(settings) else "legacy"


def explicit_legacy_engine(settings: ExecutionSettings) -> bool:
    return (
        "sequential_join_enabled" in settings.model_fields_set
        and settings.sequential_join_enabled is False
    )


def apply_engine_mode_pin(settings: ExecutionSettings, mode: object) -> ExecutionSettings:
    """Return settings whose explicit flag reflects an immutable deployment pin."""
    value = getattr(mode, "value", mode)
    if value not in {"legacy", "token"}:
        raise ValueError(f"unknown deployment engine mode: {value!r}")
    return settings.model_copy(update={"sequential_join_enabled": value == "token"})
