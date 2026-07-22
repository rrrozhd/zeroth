"""Structured compatibility warnings for authored graph settings."""

from __future__ import annotations

import json
import warnings
from typing import Literal


class LegacyEngineDeprecationWarning(DeprecationWarning):
    """Machine-readable warning emitted when legacy execution is explicit."""

    code = "legacy_engine_deprecated"
    engine_mode = "legacy"

    def __init__(self, *, stage: Literal["graph_validation", "deployment_publication"]):
        self.stage = stage
        super().__init__(
            json.dumps(
                {
                    "code": self.code,
                    "engine_mode": self.engine_mode,
                    "stage": stage,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )


def warn_legacy_engine(
    *, stage: Literal["graph_validation", "deployment_publication"], stacklevel: int = 2
) -> None:
    """Emit the structured warning through Python's standard warning channel."""
    warnings.warn(LegacyEngineDeprecationWarning(stage=stage), stacklevel=stacklevel)
