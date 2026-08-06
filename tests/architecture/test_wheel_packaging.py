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

#: Prefixes that must not appear anywhere in the distribution. The retired
#: ``zeroth/core/`` and ``zeroth/econ_plane/`` trees join this tuple in the
#: commit that deletes them, together with the installed-wheel ``find_spec``
#: guard -- asserting their absence before then would just be a red test
#: describing work that has not happened yet.
FORBIDDEN_PREFIXES = (
    "zeroth/examples/",
    "zeroth/demos/",
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
