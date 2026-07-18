"""The validation package must be importable without a warm ``zeroth.core``.

These run in subprocesses on purpose. ``tests/conftest.py`` imports
``zeroth.core.service.bootstrap`` at collection time, so by the time any
in-process test runs ``zeroth.core`` is already in ``sys.modules`` and a cycle
between the canonical package and ``zeroth.core`` is structurally invisible --
it would pass the entire suite.

The cycle is easy to reintroduce here: the contract validators import graph
models and issue types, which live under ``zeroth.core.graph``, whose package
``__init__`` exports ``GraphRepository``. If the repository ever imports the
validator eagerly again, ``zeroth.core.graph``'s own init reaches back into a
half-initialized validation package and the canonical import dies.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


COLD_IMPORTS = (
    # Canonical-package-first: what a library consumer does.
    "from zeroth.contracts.graph.validation.issues import append_issue",
    "from zeroth.contracts.graph.validation.references import validate_graph_refs",
    # Legacy-path-first: what existing callers do.
    "import zeroth.core.graph.validation",
    # Package-init-first: the edge that closes the cycle if it goes eager.
    "import zeroth.core.graph",
)


@pytest.mark.parametrize("statement", COLD_IMPORTS)
def test_imports_in_a_cold_interpreter(statement: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"not cold-importable: {statement}\n{result.stderr}"
