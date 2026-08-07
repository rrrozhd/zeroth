from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_release_demo_proves_governance_and_causality() -> None:
    source = (ROOT / "examples/27_langgraph_release.py").read_text(encoding="utf-8")
    assert "_tool_guard" not in source and "_tool_normalize" not in source
    assert "StateGraph" in source and "SqliteSaver" in source and "ApprovalCoordinator" in source

    result = subprocess.run(
        [sys.executable, "examples/27_langgraph_release.py", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["audit_decisions"] == ["allow", "deny", "require_approval", "approve"]
    assert evidence["allow_body_executions"] == 1
    assert evidence["denied_body_executions"] == 0
    assert evidence["approved_body_executions_before_resume"] == 0
    assert evidence["approved_body_executions_after_resume"] == 1
    assert evidence["approval_state"] == "resolved"
    assert evidence["causal_ancestry_valid"] is True
    assert evidence["stream_ordering"] == [1, 2, 3]
    assert evidence["thread_id"] == "demo-thread"
    assert evidence["checkpointer"] == "SqliteSaver"
    assert evidence["resume_api"] == "Command(resume=...)"
