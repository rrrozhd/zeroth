"""Execute the promotion workflow's artifact guard against real file sets."""

from __future__ import annotations

import re
import subprocess
import sys

import pytest
import yaml

from .conftest import ROOT

WORKFLOW = ROOT / ".github/workflows/promote-zeroth-platform.yml"
VERSION = "0.25.7.3"
EXPECTED = {
    f"{package}-{VERSION}{suffix}"
    for package in ("zeroth_platform", "zeroth_core")
    for suffix in ("-py3-none-any.whl", ".tar.gz")
}


def _run_guard(tmp_path, names):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(
        step
        for step in workflow["jobs"]["promote"]["steps"]
        if step["name"] == "Download and bind the immutable candidate"
    )
    guard = re.findall(r"python - <<'PY'\n(.*?)\nPY", step["run"], re.DOTALL)[-1]
    (tmp_path / "dist").mkdir()
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{VERSION}"\n')
    for name in names:
        (tmp_path / "dist" / name).touch()
    return subprocess.run(
        [sys.executable, "-c", guard], cwd=tmp_path, capture_output=True, text=True
    )


def test_matching_platform_and_compatibility_artifacts_pass(tmp_path):
    result = _run_guard(tmp_path, EXPECTED)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("mutation", ["missing_compat", "extra", "wrong_version"])
def test_incomplete_or_unexpected_artifacts_fail(tmp_path, mutation):
    names = set(EXPECTED)
    if mutation == "missing_compat":
        names = {name for name in names if name.startswith("zeroth_platform-")}
    elif mutation == "extra":
        names.add("zeroth_sdk-0.1.0-py3-none-any.whl")
    else:
        names.remove(f"zeroth_core-{VERSION}.tar.gz")
        names.add("zeroth_core-0.0.1.tar.gz")
    assert _run_guard(tmp_path, names).returncode != 0
