from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WARNING = "Gateway-only mode cannot enforce internal Agent Server tool calls."


def test_readme_first_screen_has_capability_matrix_and_warning() -> None:
    first_screen = (ROOT / "README.md").read_text(encoding="utf-8")[:5000]
    assert "| Capability | Observed | Partial | Enforced |" in first_screen
    assert WARNING in first_screen
    assert first_screen.index(WARNING) < first_screen.index("## Quickstart")


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
        "docker compose run --rm zeroth zeroth-core seed-demo",
        "gateway-smoke",
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
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert validation.returncode == 0, validation.stderr
