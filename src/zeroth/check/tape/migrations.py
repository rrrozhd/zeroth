"""Exact-version curated tape dispatch and predecessor migration registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zeroth.check.tape.models import TapeV1
from zeroth.check.tape.normalization import NormalizationError, canonical_loads

MIGRATIONS: dict[tuple[str, str], Any] = {}


class UnsupportedTapeVersionError(ValueError):
    """An artifact is not a supported curated tape version."""


def load_tape(path: str | Path) -> TapeV1:
    try:
        payload = canonical_loads(Path(path).read_bytes())
    except (OSError, NormalizationError) as exc:
        raise UnsupportedTapeVersionError("cannot load tape artifact") from exc
    if type(payload) is not dict or payload.get("schema_version") != "tape.v1":
        raise UnsupportedTapeVersionError("only tape.v1 is supported")
    return TapeV1.model_validate(payload)
