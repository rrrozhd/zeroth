"""The retention package must be importable from a cold interpreter.

These run in subprocesses on purpose. ``tests/conftest.py`` imports
``zeroth.service.bootstrap`` at collection time, so by the time any
in-process test runs most of the service graph is already in ``sys.modules``
and a cycle between these packages is structurally invisible --
it would pass the entire suite.

The cycle is one line away here in both directions. Every extracted module
imports the manifest and state models, which stay under
``zeroth.core.retention``, whose package ``__init__`` publishes the erasure
service. If that init resolves the service eagerly, importing any canonical
module reaches back into a half-initialized package and the import dies.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

COLD_IMPORTS = (
    # Canonical-package-first: what a library consumer does.
    "from zeroth.governance.retention.manifests import build_cleanup_manifest",
    "from zeroth.governance.retention.replay import replay_cleanup_state",
    "import zeroth.governance.retention",
    "from zeroth.governance.retention import RetentionErasureService",
    "from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser",
    # Legacy-path-first: what every existing caller does.
    # Package-init-first: the edge that closes the cycle if it goes eager.
)


@pytest.mark.parametrize("statement", COLD_IMPORTS)
def test_imports_in_a_cold_interpreter(statement: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"not cold-importable: {statement}\n{result.stderr}"
