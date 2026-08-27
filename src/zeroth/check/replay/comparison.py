"""Exact three-run trajectory quorum facts."""

from __future__ import annotations

from collections.abc import Iterable

from zeroth.check.replay.models import QuorumSummary


def compare_three(baseline: bytes, candidates: Iterable[bytes]) -> QuorumSummary:
    runs = tuple(candidates)
    if len(runs) != 3:
        raise ValueError("V1 ordinary comparison requires exactly three runs")
    matches = sum(candidate == baseline for candidate in runs)
    return QuorumSummary(3, matches, 2, matches >= 2)
