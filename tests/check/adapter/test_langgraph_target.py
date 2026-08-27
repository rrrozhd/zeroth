from __future__ import annotations

import pytest

from zeroth.check.adapter.langgraph import LangGraphCheckTarget, TargetInvocationError


def test_rejects_blank_logical_case_or_scenario(tmp_path) -> None:
    target = LangGraphCheckTarget(
        graph_factory=lambda checkpointer: object(),
        checkpointer_factory=lambda path: object(),
        case_input=lambda case: {},
        invocation_config=lambda case, run: {},
    )
    with pytest.raises(TargetInvocationError):
        target.invoke(case="", scenario_run_id="run", checkpointer_path=tmp_path / "a.sqlite")
