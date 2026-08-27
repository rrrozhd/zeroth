from __future__ import annotations

import pytest

from release.live_evaluation.code_units import loop_demo


def test_quality_inspect_applies_a_bounded_evaluation_delay(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(loop_demo.time, "sleep", sleeps.append)

    result = loop_demo.quality_inspect(
        {"records": [], "evaluation_delay_ms": 8_000}
    )

    assert sleeps == [8.0]
    assert result["evaluation_delay_ms"] == 8_000


@pytest.mark.parametrize("value", [-1, 8_001, True, "10"])
def test_quality_inspect_rejects_an_invalid_evaluation_delay(value: object) -> None:
    with pytest.raises(ValueError, match="evaluation_delay_ms"):
        loop_demo.quality_inspect({"records": [], "evaluation_delay_ms": value})
