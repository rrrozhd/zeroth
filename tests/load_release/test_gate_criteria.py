"""Load-gate results are named criteria evaluated by the harness, not shell policy."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from release.load import harness
from release.load.report import GATE_CRITERIA, accepted_runs_intact, thresholds_hold

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/release-gates.yml"
RECORD_STEP = "Run product profiles and record every independent result"


def _passing_report() -> dict:
    return {
        "evaluation": {"latency_p95_ms": True, "lost_accepted_runs": True},
        "measurements": {
            "sustained": {"lost_accepted_runs": 0, "duplicate_accepted_runs": 0},
            "burst": {"lost_accepted_runs": 0, "duplicate_accepted_runs": 0},
        },
    }


def test_thresholds_hold_only_when_every_evaluated_rule_passed() -> None:
    assert thresholds_hold(_passing_report())
    assert not thresholds_hold({**_passing_report(), "evaluation": {"latency_p95_ms": False}})
    assert not thresholds_hold({**_passing_report(), "evaluation": {}})
    assert not thresholds_hold({"passed": True})
    assert not thresholds_hold(["not", "a", "report"])


@pytest.mark.parametrize("counter", ["lost_accepted_runs", "duplicate_accepted_runs"])
def test_accepted_runs_intact_rejects_any_lost_or_duplicated_run(counter: str) -> None:
    report = _passing_report()
    assert accepted_runs_intact(report)
    report["measurements"]["burst"][counter] = 1
    assert not accepted_runs_intact(report)
    assert not accepted_runs_intact({"measurements": {}})
    assert not accepted_runs_intact({"measurements": {"burst": "opaque"}})
    assert not accepted_runs_intact(None)


def test_gate_criteria_name_the_two_recorded_results() -> None:
    assert set(GATE_CRITERIA) == {"thresholds", "accepted-run-integrity"}


@pytest.mark.parametrize("criterion", sorted(GATE_CRITERIA))
def test_check_command_exit_code_follows_the_criterion(tmp_path: Path, criterion: str) -> None:
    report = tmp_path / "benchmark.json"
    report.write_text(json.dumps(_passing_report()))
    assert harness.main(["check", "--report", str(report), "--criterion", criterion]) == 0
    failing = _passing_report()
    failing["evaluation"]["latency_p95_ms"] = False
    failing["measurements"]["burst"]["lost_accepted_runs"] = 2
    report.write_text(json.dumps(failing))
    assert harness.main(["check", "--report", str(report), "--criterion", criterion]) == 1


def test_check_command_fails_closed_on_a_missing_or_unreadable_report(tmp_path: Path) -> None:
    missing = tmp_path / "absent.json"
    assert harness.main(["check", "--report", str(missing), "--criterion", "thresholds"]) == 1
    missing.write_text("{not json")
    assert harness.main(["check", "--report", str(missing), "--criterion", "thresholds"]) == 1


def _record_step_script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["load-recovery"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == RECORD_STEP)


def test_record_step_delegates_every_result_to_a_tested_program() -> None:
    """No pass/fail policy is embedded in the workflow shell."""
    script = _record_step_script()
    assert re.search(r"python\s+-\s+<<", script) is None, "inline Python policy in the gate step"
    assert "harness.py check" in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


@pytest.mark.parametrize(
    ("thresholds", "integrity"), [(0, 0), (1, 0), (0, 1)], ids=["green", "thresholds", "integrity"]
)
def test_record_step_wires_each_check_exit_code_to_its_named_result(
    tmp_path: Path, thresholds: int, integrity: int
) -> None:
    """Execute the real step shell with stubbed tools and read the recorded results."""
    script = _record_step_script()
    commands = tmp_path / "bin"
    commands.mkdir()
    log = tmp_path / "calls.log"
    (commands / "uv").write_text(
        "#!/bin/bash\n"
        'echo "uv $*" >> "$CALL_LOG"\n'
        'if [[ "$*" == *"harness.py check"* ]]; then\n'
        '  [[ "$*" == *"--criterion thresholds"* ]] && exit "$STUB_THRESHOLDS"\n'
        '  [[ "$*" == *"--criterion accepted-run-integrity"* ]] && exit "$STUB_INTEGRITY"\n'
        "  exit 3\n"
        "fi\n"
        "exit 0\n"
    )
    (commands / "python").write_text("#!/bin/bash\n" 'echo "python $*" >> "$CALL_LOG"\n')
    for stub in commands.iterdir():
        stub.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "STUB_THRESHOLDS": str(thresholds),
            "STUB_INTEGRITY": str(integrity),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    record = next(line for line in log.read_text().splitlines() if "cli.py record" in line)
    status = {0: "passed", 1: "failed"}
    assert f"thresholds={status[thresholds]}" in record
    assert f"accepted-run-integrity={status[integrity]}" in record
    assert "--gate load-recovery" in record
    assert result.returncode == 0, result.stderr
