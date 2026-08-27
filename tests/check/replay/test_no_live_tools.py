from __future__ import annotations

from zeroth.check.adapter.bindings import CheckBindings
from zeroth.check.adapter.loading import load_target
from zeroth.check.replay.matcher import ReplayMatcher
from zeroth.check.replay.tools import ReplayToolFactory
from zeroth.integrations.langgraph import SQLiteActionExecutionRepository

from .helpers import Repository, replay_tape


def test_real_tool_node_uses_tape_without_retaining_or_executing_live_tool(
    tmp_path, monkeypatch
) -> None:
    marker = tmp_path / "live-marker"
    monkeypatch.setenv("ZEROTH_CHECK_LIVE_MARKER", str(marker))
    tape = replay_tape()
    matcher = ReplayMatcher(tape)
    bindings = CheckBindings(
        action_repository=SQLiteActionExecutionRepository(tmp_path / "actions.sqlite"),
        mode="replay",
        replacements={"charge": ReplayToolFactory(matcher)},
    )
    target = load_target("tests.check.fixtures.targets.replay:build_target", bindings)
    result = target.invoke(
        case="7", scenario_run_id="logical-1", checkpointer_path=tmp_path / "checkpoint.sqlite"
    )
    assert result["messages"][-1].content == '{"charged": 7}'
    assert matcher.finish().facts == ()
    assert bindings.registrations["charge"].implementation is None
    assert not marker.exists()
