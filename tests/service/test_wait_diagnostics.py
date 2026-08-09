"""Regression tests for the wait helper's diagnostics.

These exist because ZER-21's only captured failure said just "timed out waiting for
condition", which cannot distinguish a slow arrival from a stalled or failed run. If a
future edit silences these diagnostics, the next occurrence becomes undiagnosable again —
so the behaviour is pinned rather than left to survive on good intentions.
"""

from __future__ import annotations

import json
import warnings

import pytest

from tests.service.helpers import SLOW_WAIT_LOG_ENV, wait_for


def test_a_slow_but_successful_wait_is_warned_and_recorded(tmp_path, monkeypatch) -> None:
    log = tmp_path / "slow-waits.jsonl"
    monkeypatch.setenv(SLOW_WAIT_LOG_ENV, str(log))
    calls = {"n": 0}

    def eventually() -> bool:
        calls["n"] += 1
        return calls["n"] > 8

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        wait_for(eventually, slow_after=0.02, describe=lambda: "status='running'")

    assert any("satisfied only after" in str(w.message) for w in caught)
    assert any("status='running'" in str(w.message) for w in caught)

    record = json.loads(log.read_text().splitlines()[-1])
    assert record["polls"] >= 8
    assert record["elapsed"] > 0.02
    assert record["observed"] == "status='running'"


def test_a_prompt_wait_is_neither_warned_nor_recorded(tmp_path, monkeypatch) -> None:
    log = tmp_path / "slow-waits.jsonl"
    monkeypatch.setenv(SLOW_WAIT_LOG_ENV, str(log))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        wait_for(lambda: True, slow_after=5.0)

    assert not caught
    assert not log.exists()


def test_a_timeout_reports_elapsed_polls_and_observed_state() -> None:
    with pytest.raises(AssertionError) as excinfo:
        wait_for(lambda: False, timeout=0.05, describe=lambda: "status='pending'")
    message = str(excinfo.value)
    assert "timed out waiting for condition after" in message
    assert "polls)" in message
    assert "status='pending'" in message


def test_a_describer_that_raises_cannot_mask_the_timeout() -> None:
    with pytest.raises(AssertionError) as excinfo:
        wait_for(lambda: False, timeout=0.05, describe=lambda: 1 / 0)
    assert "state unavailable" in str(excinfo.value)
    assert "ZeroDivisionError" in str(excinfo.value)


def test_recording_failure_never_breaks_the_wait(tmp_path, monkeypatch) -> None:
    # An unwritable location must not turn a passing wait into a failure. A regular file
    # standing where a parent directory is expected makes mkdir fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv(SLOW_WAIT_LOG_ENV, str(blocker / "sub" / "x.jsonl"))
    calls = {"n": 0}

    def eventually() -> bool:
        calls["n"] += 1
        return calls["n"] > 8

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        wait_for(eventually, slow_after=0.02)
