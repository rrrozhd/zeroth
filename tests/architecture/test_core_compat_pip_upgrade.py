"""Exercise the documented migration with real pip and overlapping old files."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib
import zipfile

import pytest

from tests.architecture import test_wheel_packaging

platform_wheel = test_wheel_packaging.wheel

ROOT = Path(__file__).resolve().parents[2]


def _run(command, directory):
    result = subprocess.run(command, cwd=directory, capture_output=True, text=True)
    assert result.returncode == 0, f"{command}\n{result.stdout}\n{result.stderr}"
    return result.stdout


def _legacy_real_file_wheel(directory):
    """A minimal old distribution with genuine wheel ownership/RECORD entries."""
    name = "zeroth_core-0.25.7.2"
    metadata = f"{name}.dist-info"
    files = {
        "zeroth/service/__init__.py": b"LEGACY_REAL_FILE = True\n",
        f"{metadata}/METADATA": b"Metadata-Version: 2.1\nName: zeroth-core\nVersion: 0.25.7.2\n",
        f"{metadata}/WHEEL": b"Wheel-Version: 1.0\nGenerator: upgrade-regression\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = io.StringIO()
    writer = csv.writer(record)
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow([path, f"sha256={digest}", len(content)])
    writer.writerow([f"{metadata}/RECORD", "", ""])
    files[f"{metadata}/RECORD"] = record.getvalue().encode()
    wheel = directory / f"{name}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return wheel


def _documented_upgrade_commands():
    readme = (ROOT / "packaging/core-compat/README.md").read_text()
    heading = "## Upgrading an existing installation"
    if heading not in readme:
        # The original README promises in-place migration through the redirect.
        return [["install", "--upgrade", "zeroth-core"]]
    section = readme.split(heading, 1)[1].split("\n## ", 1)[0]
    commands = re.findall(r"^python -m pip (.+)$", section, re.MULTILINE)
    assert commands, "the migration guide must contain executable pip commands"
    return [shlex.split(command) for command in commands]


@pytest.mark.parametrize(
    "initial_layout", ["legacy_only", "platform_already_installed", "failed_upgrade"]
)
def test_documented_pip_upgrade_preserves_real_platform_files(
    tmp_path, platform_wheel, initial_layout
):
    output = tmp_path / "compat-wheel"
    _run(["uv", "build", "packaging/core-compat", "--wheel", "--out-dir", str(output)], ROOT)
    compatibility = next(output.glob("*.whl"))
    legacy = _legacy_real_file_wheel(tmp_path)
    environment = tmp_path / "consumer"
    _run([sys.executable, "-m", "venv", str(environment)], tmp_path)
    python = environment / "bin/python"
    pip = [str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check"]
    print(_run([*pip, "--version"], tmp_path).strip())
    _run([*pip, "install", "--no-index", "--no-deps", str(legacy)], tmp_path)
    _run(
        [str(python), "-I", "-c", "import zeroth.service; assert zeroth.service.LEGACY_REAL_FILE"],
        tmp_path,
    )
    if initial_layout != "legacy_only":
        candidates = [str(platform_wheel)]
        if initial_layout == "failed_upgrade":
            candidates.append(str(compatibility))
        print(
            _run([*pip, "install", "--no-index", "--no-deps", "--upgrade", *candidates], tmp_path)
        )
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    wheels = {"zeroth-core": compatibility, "zeroth-platform": platform_wheel}
    for command in _documented_upgrade_commands():
        assert command[0] in {"install", "uninstall"}
        if command[0] == "install":
            resolved = []
            for argument in command[1:]:
                name, _, pin = argument.partition("==")
                if name in wheels:
                    assert not pin or pin == version, "documented migration pin is stale"
                    resolved.append(str(wheels[name]))
                else:
                    assert argument.startswith("-"), f"unexpected package: {argument}"
                    resolved.append(argument)
            # Both exact local candidates are explicit, so the real pip
            # installation ordering runs without network or runtime dependencies.
            if str(compatibility) in resolved and str(platform_wheel) not in resolved:
                resolved.append(str(platform_wheel))
            command = ["install", "--no-index", "--no-deps", *resolved]
        print(_run([*pip, *command], tmp_path))
    _run(
        [
            str(python),
            "-I",
            "-c",
            "from importlib.metadata import version; import zeroth.service; "
            "assert zeroth.service.__file__, 'pip removed the platform package initializer'; "
            "assert not hasattr(zeroth.service, 'LEGACY_REAL_FILE'); "
            f"assert version('zeroth-platform') == version('zeroth-core') == {version!r}",
        ],
        tmp_path,
    )
    with zipfile.ZipFile(platform_wheel) as archive:
        expected = hashlib.sha256(archive.read("zeroth/service/__init__.py")).hexdigest()
    actual = _run(
        [
            str(python),
            "-I",
            "-c",
            "import hashlib, pathlib, zeroth.service; "
            "print(hashlib.sha256(pathlib.Path(zeroth.service.__file__).read_bytes()).hexdigest())",
        ],
        tmp_path,
    )
    assert actual.strip() == expected
