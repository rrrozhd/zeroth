"""Canonical JSON verdict renderer."""

from zeroth.check.tape.normalization import canonical_bytes
from zeroth.check.verdict.models import CheckVerdict


def render_json(verdict: CheckVerdict) -> bytes:
    return canonical_bytes(verdict.model_dump(mode="json")) + b"\n"
