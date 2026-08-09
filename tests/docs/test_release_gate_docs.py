"""R9 — the release-gate guide identifies every responsibility and its commands run."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs/how-to/deployment/release-gates.md"
MANIFEST = ROOT / "release/gates/release-gates.json"
CLI = ROOT / "release/gates/cli.py"

TEXT = GUIDE.read_text(encoding="utf-8")


def _manifest() -> dict:
    import json

    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "responsibility", ["Pull request", "Nightly", "Release candidate", "Manual"]
)
def test_the_guide_identifies_each_trigger_responsibility(responsibility):
    assert f"### {responsibility}" in TEXT


def test_the_guide_names_every_gate_in_the_manifest():
    for gate in _manifest()["gates"]:
        assert gate["title"] in TEXT, f"the guide never mentions the {gate['id']} gate"


def test_the_guide_explains_every_refusal_reason():
    for status in ("missing", "stale", "partial", "mismatched", "failed"):
        assert f"`{status}`" in TEXT, f"the guide does not explain a {status} verdict"


def test_the_guide_states_where_the_manual_signoff_lives():
    assert "release/signoff/" in TEXT
    # The path the release workflow actually reads must be the documented one.
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/release-zeroth-core.yml").read_text(encoding="utf-8")
    )
    scripts = "\n".join(
        step.get("run", "")
        for job in workflow["jobs"].values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict)
    )
    assert "release/signoff/" in scripts


def test_the_guide_is_published_in_the_navigation():
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")

    assert "how-to/deployment/release-gates.md" in nav


def _documented_commands() -> list[str]:
    blocks = re.findall(r"```bash\n(.*?)```", TEXT, flags=re.DOTALL)
    return [
        line.strip()
        for block in blocks
        for line in block.splitlines()
        if line.strip().startswith("python release/gates/cli.py")
    ]


def test_the_guide_documents_commands_at_all():
    assert _documented_commands(), "the guide states no runnable command"


@pytest.mark.parametrize("command", _documented_commands())
def test_documented_commands_execute(command: str, tmp_path: Path):
    """The commands must be real: wrong flags or a renamed subcommand fail here."""
    argv = command.split()
    argv[0] = sys.executable
    argv[1] = str(CLI)
    # Keep the doc's own output path out of the working tree.
    argv = [str(tmp_path / "candidate-identity.json") if part.endswith(".json") else part
            for part in argv]

    if "identity" in argv:
        completed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)
        assert completed.returncode == 0, completed.stderr
        return

    # validate/verdict need an identity to read; measure one first, then assert
    # the command is well-formed and fails closed rather than erroring on usage.
    identity_path = tmp_path / "candidate-identity.json"
    measured = subprocess.run(
        [sys.executable, str(CLI), "identity", "--output", str(identity_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert measured.returncode == 0, measured.stderr

    completed = subprocess.run(
        [*argv, "--evidence-root", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "usage:" not in completed.stderr, f"the guide states a malformed command: {command}"
    assert "missing" in completed.stdout
