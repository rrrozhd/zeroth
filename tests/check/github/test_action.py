from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[3]


def test_composite_action_has_no_write_permissions_and_one_summary_owner() -> None:
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert set(action["inputs"]) == {"config", "report-dir", "fail-on"}
    assert "permissions" not in action
    owners = [step for step in action["runs"]["steps"] if "zeroth_check_action.py" in step["run"]]
    assert len(owners) == 1


def _run(tmp_path: Path, exit_code: int, *, fail_on: str = "block,invalid") -> tuple[int, str]:
    summary = tmp_path / "github-summary.md"
    fake = ROOT / "tests/check/github/fake_cli.py"
    environment = os.environ | {
        "GITHUB_STEP_SUMMARY": str(summary),
        "FAKE_CHECK_EXIT": str(exit_code),
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/zeroth_check_action.py"),
            "--config",
            "fixture.yaml",
            "--report-dir",
            str(tmp_path / "reports"),
            "--fail-on",
            fail_on,
            "--cli",
            f"{sys.executable} {fake}",
        ],
        env=environment,
        check=False,
    )
    return completed.returncode, summary.read_text()


def test_default_allows_canary_and_preserves_block_invalid(tmp_path) -> None:
    assert _run(tmp_path / "canary", 10)[0] == 0
    assert _run(tmp_path / "block", 20)[0] == 20
    assert _run(tmp_path / "invalid", 30)[0] == 30
    code, summary = _run(tmp_path / "pass", 0)
    assert code == 0
    assert summary.count("# one summary") == 1


def test_configured_canary_failure_preserves_ten(tmp_path) -> None:
    assert _run(tmp_path, 10, fail_on="canary,block,invalid")[0] == 10
