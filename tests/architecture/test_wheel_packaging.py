"""What the built wheel actually ships.

Every other guard in this suite reasons about the source tree. A wheel is a
different artifact: files can be present in ``src`` and absent from the
distribution, or the reverse, and the difference only shows up on a consumer's
machine. ZER-25 relocates typing metadata, vendored licensing, the demo seeder
and the migration tree, and moves two example trees *out* of the wheel -- so the
distribution is inspected directly rather than inferred.

The wheel is built once per session; a build is slow and nothing here mutates it.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]

#: Paths the wheel must ship. Each one is a resource that a pure-Python import
#: check would miss: typing metadata, vendored attribution, and package data.
REQUIRED_ENTRIES = (
    "zeroth/py.typed",
    "zeroth/contracts/governed/LICENSE",
    "zeroth/contracts/governed/PROVENANCE.md",
    "zeroth/service/demo.py",
    "zeroth/service/cli.py",
    "zeroth/service/_migrations/env.py",
    "zeroth/service/_migrations/versions/001_initial_schema.py",
)

#: Prefixes that must not appear anywhere in the distribution.
FORBIDDEN_PREFIXES = (
    "zeroth/core/",
    "zeroth/econ_plane/",
    "zeroth/examples/",
    "zeroth/demos/",
)

CANONICAL_WHEEL_SOURCES = (
    "src/zeroth/contracts",
    "src/zeroth/econ",
    "src/zeroth/eval",
    "src/zeroth/governance",
    "src/zeroth/integrations",
    "src/zeroth/platform",
    "src/zeroth/runtime",
    "src/zeroth/service",
    "src/zeroth/py.typed",
)


@pytest.fixture(scope="session")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel once and hand back its path."""
    output = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover - a broken build fails loudly
        pytest.fail(f"uv build --wheel failed:\n{result.stdout}\n{result.stderr}")
    built = sorted(output.glob("*.whl"))
    assert len(built) == 1, f"expected exactly one wheel, got {built}"
    return built[0]


@pytest.fixture(scope="session")
def wheel_names(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


@pytest.mark.parametrize("entry", REQUIRED_ENTRIES)
def test_the_wheel_ships_every_required_resource(entry: str, wheel_names: list[str]) -> None:
    """Non-Python resources regress silently; the ZIP listing is the evidence."""
    assert entry in wheel_names


@pytest.mark.parametrize("prefix", FORBIDDEN_PREFIXES)
def test_the_wheel_ships_no_retired_or_example_tree(prefix: str, wheel_names: list[str]) -> None:
    """Retired packages and repository-only examples stay out of the distribution."""
    shipped = sorted(name for name in wheel_names if name.startswith(prefix))

    assert not shipped, f"wheel ships {prefix}:\n  " + "\n  ".join(shipped)


def test_wheel_members_come_from_explicit_tracked_sources(wheel_names: list[str]) -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel_config = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert wheel_config.get("only-include") == list(CANONICAL_WHEEL_SOURCES)
    assert wheel_config.get("sources") == ["src"]

    tracked = subprocess.run(
        ["git", "ls-files", "-z", "src/zeroth"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    missing_sources = sorted(
        name
        for name in wheel_names
        if name
        and not name.endswith("/")
        and ".dist-info/" not in name
        and f"src/{name}" not in tracked
    )

    assert not missing_sources, "wheel members have no tracked src/ file:\n  " + "\n  ".join(
        missing_sources
    )


def test_the_wheel_does_not_ship_release_tooling(wheel_names: list[str]) -> None:
    shipped = sorted(name for name in wheel_names if name.startswith("release/"))

    assert not shipped, "wheel ships release tooling:\n  " + "\n  ".join(shipped)


def test_find_spec_resolves_no_retired_package_against_the_installed_wheel(
    wheel: Path, tmp_path: Path
) -> None:
    """A consumer installing the wheel cannot import the retired trees.

    Unpacking and pointing an interpreter at the result exercises exactly what
    import resolution does with an installed distribution, without paying for a
    virtualenv and a dependency solve.
    """
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib.util\n"
            "resolved = [\n"
            "    name\n"
            "    for name in ('zeroth.core', 'zeroth.econ_plane')\n"
            "    if importlib.util.find_spec(name) is not None\n"
            "]\n"
            "assert not resolved, resolved\n",
        ],
        cwd=site,
        env={"PYTHONPATH": str(site), "PATH": ""},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"retired packages resolve from the wheel:\n{result.stderr}"
