"""The validation package must be importable from a cold interpreter.

These run in subprocesses on purpose. ``tests/conftest.py`` imports
``zeroth.service.bootstrap`` at collection time, so by the time any
in-process test runs most of the service graph is already in ``sys.modules``
and a cycle between these packages is structurally invisible --
it would pass the entire suite.

The cycle is easy to reintroduce here: the contract validators import graph
models and issue types, which live under ``zeroth.contracts.graph``, whose package
``__init__`` exports ``GraphRepository``. If the repository ever imports the
validator eagerly again, ``zeroth.contracts.graph``'s own init reaches back into a
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
    "from zeroth.contracts.graph.validation import ContractValidator",
    # The public validator, composed in the runtime layer.
    "from zeroth.runtime.graph_validation import GraphValidator",
    # Legacy-path-first: what existing callers do. Resolution is lazy, so this
    # also proves the shim's deferred runtime import survives a cold start.
    # Package-init-first: the edge that closes the cycle if it goes eager.
    "import zeroth.contracts.graph",
    # Legacy package-init-first: the compatibility shell re-exports the same
    # objects, so its init must survive a cold start too.
)


@pytest.mark.parametrize("statement", COLD_IMPORTS)
def test_imports_in_a_cold_interpreter(statement: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"not cold-importable: {statement}\n{result.stderr}"
