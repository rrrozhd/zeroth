from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.release_inputs import requires_release_inputs


ROOT = Path(__file__).resolve().parents[2]
WARNING = "Gateway-only mode cannot enforce internal Agent Server tool calls."


def test_readme_explains_langgraph_enforcement_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert WARNING in readme
    assert "govern_tools" in readme
    assert "ZerothMiddleware" in readme


@requires_release_inputs(
    "release/langgraph/benchmark-baseline-0.16.1.7.json",
    "release/langgraph/benchmark-evidence.json",
)
def test_canonical_guide_covers_release_operations_and_commands_execute(tmp_path: Path) -> None:
    guide = (ROOT / "docs/how-to/deployment/langgraph-release.md").read_text(encoding="utf-8")
    for marker in (
        "clean install",
        "managed",
        "self-hosted",
        "1.2.9",
        "0.11.1",
        "interrupt",
        "idempotency",
        "outage",
        "redaction",
        "arbitrary interrupts",
        "resources",
        "environment variables",
        "docker compose run --rm zeroth zeroth seed-demo",
        "gateway-smoke",
        "validate --phase final",
    ):
        assert marker.lower() in guide.lower(), marker

    demo = subprocess.run(
        [sys.executable, "examples/27_langgraph_release.py", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert demo.returncode == 0, demo.stderr
    validation = subprocess.run(
        [
            sys.executable,
            "release/langgraph/harness.py",
            "validate",
            "--manifest",
            "release/langgraph/release-manifest.json",
            "--phase",
            "source",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert validation.returncode == 0, validation.stderr
