from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_frontend_version.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_generator_writes_deterministic_typescript_and_checks_drift(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    output = tmp_path / "version.ts"
    pyproject.write_text('[project]\nname = "zeroth-core"\nversion = "9.8.7"\n')

    generated = _run("--pyproject", str(pyproject), "--out", str(output))

    assert generated.returncode == 0, generated.stderr
    assert output.read_text() == (
        "// Generated from pyproject.toml; run `npm run gen:version`.\n"
        'export const VERSION = "9.8.7";\n'
    )
    assert _run("--pyproject", str(pyproject), "--out", str(output), "--check").returncode == 0

    output.write_text('export const VERSION = "stale";\n')
    drift = _run("--pyproject", str(pyproject), "--out", str(output), "--check")
    assert drift.returncode == 1
    assert "DRIFT" in drift.stderr
