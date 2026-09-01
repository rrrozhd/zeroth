from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "release/gates/cli.py"


def test_validate_can_prove_one_named_gate(candidate, evidence) -> None:
    identity = evidence / "candidate.json"
    identity.write_text(json.dumps(candidate), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "validate",
            "--identity",
            str(identity),
            "--evidence-root",
            str(evidence),
            "--gate",
            "remote-acceptance",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "remote-acceptance: passed" in result.stdout
