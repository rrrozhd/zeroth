from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "scripts" / "dump_openapi.py"
REGULUS = ROOT / "scripts" / "dump_regulus_openapi.py"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_main_generator_uses_canonical_service_import() -> None:
    source = MAIN.read_text()
    assert "from zeroth.service.app import create_app" in source
    assert "from zeroth.core.service.app import create_app" not in source


def test_main_generator_is_deterministic_and_detects_drift(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert _run(MAIN, "--out", str(first)).returncode == 0
    assert _run(MAIN, "--out", str(second)).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    assert _run(MAIN, "--out", str(first), "--check").returncode == 0
    first.write_text("{}\n")
    drift = _run(MAIN, "--out", str(first), "--check")
    assert drift.returncode == 1
    assert "DRIFT" in drift.stderr


def test_parent_schema_exposes_proxy_but_not_mounted_regulus_routes(tmp_path: Path) -> None:
    output = tmp_path / "main.json"
    assert _run(MAIN, "--out", str(output)).returncode == 0
    paths = json.loads(output.read_text())["paths"]
    assert "/v1/econ/regulus/dashboard/kpis" in paths
    assert not any(path.startswith("/regulus/") for path in paths)


def test_regulus_generator_is_deterministic_and_detects_drift(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    generated = _run(REGULUS, "--out", str(first))
    assert generated.returncode == 0, generated.stderr
    assert _run(REGULUS, "--out", str(second)).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    assert "/v1/dashboard/kpis" in json.loads(first.read_text())["paths"]
    assert _run(REGULUS, "--out", str(first), "--check").returncode == 0
    first.write_text("{}\n")
    drift = _run(REGULUS, "--out", str(first), "--check")
    assert drift.returncode == 1
    assert "DRIFT" in drift.stderr
