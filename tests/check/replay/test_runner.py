from __future__ import annotations

from zeroth.check.replay.runner import run_three
from zeroth.check.replay.worker import _exception_type_chain

from .helpers import replay_tape


def test_three_fresh_processes_match_without_live_effects(tmp_path, monkeypatch) -> None:
    marker = tmp_path / "live-marker"
    monkeypatch.setenv("ZEROTH_CHECK_LIVE_MARKER", str(marker))
    batch = run_three(
        "tests.check.fixtures.targets.replay:build_target",
        replay_tape(),
        state_root=tmp_path / "runs",
    )
    assert batch.invalid_slots == ()
    assert batch.quorum.matching_runs == 3
    assert len({run.process_id for run in batch.runs}) == 3
    assert len({run.checkpoint_path for run in batch.runs}) == 3
    assert len({run.action_repository_path for run in batch.runs}) == 3
    assert all(run.full_check_eligible is True for run in batch.runs)
    assert not marker.exists()


def test_infrastructure_diagnostics_expose_only_bounded_exception_types() -> None:
    try:
        try:
            raise ValueError("secret-value")
        except ValueError as cause:
            raise RuntimeError("raw payload") from cause
    except RuntimeError as exc:
        diagnostic = _exception_type_chain(exc)

    assert diagnostic == "RuntimeError <- ValueError"
    assert "secret-value" not in diagnostic
    assert "raw payload" not in diagnostic
