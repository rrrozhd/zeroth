"""Portable path boundaries for committed live-evaluation helpers."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_runtime_paths_default_to_the_current_checkout(monkeypatch) -> None:
    monkeypatch.delenv("ZEROTH_EVALUATION_WORKTREE", raising=False)
    monkeypatch.delenv("ZEROTH_EVALUATION_STATE_ROOT", raising=False)

    from release.live_evaluation.runtime_paths import resolve_runtime_paths

    paths = resolve_runtime_paths()

    assert paths.worktree == ROOT.resolve()
    assert paths.state_root == (
        Path.home() / ".local/share/zeroth/evaluations/evaluation-studio-v1"
    ).resolve()


def test_runtime_paths_accept_explicit_external_overrides(tmp_path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    state = tmp_path / "state"
    monkeypatch.setenv("ZEROTH_EVALUATION_WORKTREE", str(checkout))
    monkeypatch.setenv("ZEROTH_EVALUATION_STATE_ROOT", str(state))

    from release.live_evaluation.runtime_paths import resolve_runtime_paths

    paths = resolve_runtime_paths()

    assert paths.worktree == checkout.resolve()
    assert paths.state_root == state.resolve()


def test_executable_checkpoint_modules_do_not_embed_a_developer_checkout() -> None:
    offenders = []
    for path in (ROOT / "release/live_evaluation").glob("*.py"):
        if "/Users/dondoe/.codex/worktrees" in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []
