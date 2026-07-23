"""The public package must import without pulling in the optional langgraph dep.

This runs in the base suite (no ``langgraph_conformance`` marker, no
``importorskip``): importing ``zeroth.integrations.langgraph`` must succeed and
must not eagerly import ``langgraph``, which is only installed in the
``gateway-conformance`` dependency group. Verified in a clean subprocess so the
result is independent of whatever this test session already imported.
"""

from __future__ import annotations

import subprocess
import sys


def test_importing_the_package_does_not_import_langgraph() -> None:
    code = (
        "import sys, zeroth.integrations.langgraph; "
        # The package is itself named ...langgraph, so match the REAL top-level
        # package precisely rather than by substring.
        "leaked = sorted(k for k in sys.modules if k == 'langgraph' or k.startswith('langgraph.')); "
        "assert not leaked, leaked"
    )
    try:
        subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(f"langgraph was imported eagerly:\n{exc.stderr}") from exc
