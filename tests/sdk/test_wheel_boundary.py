"""The SDK wheel stays lean and owns no server or UI files."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
SDK_PROJECT = REPO_ROOT / "packaging" / "sdk"


def _build_sdk_wheel(output: Path) -> Path:
    result = subprocess.run(
        ["uv", "build", "--wheel", str(SDK_PROJECT), "--out-dir", str(output)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(output.glob("zeroth_sdk-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_sdk_wheel_contains_only_client_owned_namespaces(tmp_path: Path) -> None:
    sdk_wheel = _build_sdk_wheel(tmp_path)
    with zipfile.ZipFile(sdk_wheel) as archive:
        names = archive.namelist()

    for required in (
        "zeroth/sdk/__init__.py",
        "zeroth/protocol/__init__.py",
        "zeroth/instrumentation/__init__.py",
    ):
        assert required in names

    forbidden = (
        "zeroth/econ/plane/",
        "zeroth/service/",
        "zeroth/runtime/",
        "zeroth/platform/",
        "zeroth_console/",
    )
    assert not [name for name in names if name.startswith(forbidden)]


def test_sdk_wheel_has_only_lean_runtime_dependencies(tmp_path: Path) -> None:
    sdk_wheel = _build_sdk_wheel(tmp_path)
    with zipfile.ZipFile(sdk_wheel) as archive:
        metadata_name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode()

    requirements = {
        line.removeprefix("Requires-Dist: ").split(";", 1)[0].strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist: ")
    }
    assert requirements == {"httpx>=0.27", "pydantic>=2.10"}
    for forbidden in ("fastapi", "sqlalchemy", "alembic", "redis", "uvicorn", "aiosqlite"):
        assert forbidden not in metadata.lower()
