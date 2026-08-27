from __future__ import annotations

import pytest

from zeroth.check.replay.comparison import compare_three


@pytest.mark.parametrize(("matches", "met"), [(3, True), (2, True), (1, False), (0, False)])
def test_exact_two_of_three_quorum(matches: int, met: bool) -> None:
    runs = [b"baseline"] * matches + [f"different-{index}".encode() for index in range(3 - matches)]
    result = compare_three(b"baseline", runs)
    assert result.matching_runs == matches
    assert result.quorum_met is met


def test_rejects_any_run_count_other_than_three() -> None:
    with pytest.raises(ValueError):
        compare_three(b"baseline", [b"baseline"] * 2)
