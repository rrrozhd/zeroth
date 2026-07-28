"""The public package must import without pulling in the optional langgraph deps.

This runs in the base suite (no ``langgraph_conformance`` marker, no
``importorskip``): importing ``zeroth.integrations.langgraph`` must succeed and
must not eagerly import ``langgraph`` **or** ``langchain``, both of which ship
only in the ``gateway-conformance`` dependency group. Verified in a clean
subprocess so the result is independent of whatever this test session already
imported.

``langchain`` was added to the guard alongside ``ZerothMiddleware`` (ZER-6 T7).
The middleware needs ``langchain.agents``, so a module-scope import of it in
``__init__.py`` would sail past a langgraph-only guard and instead hard-fail the
package for every caller who installed without the optional group.

**The prefix has to be exact.** ``langchain_core`` is a *core* dependency that
``_wrapper.py``, ``_handler.py`` and ``_callbacks.py`` all import eagerly and
legitimately, and it starts with the six letters ``langch`` -- so the guard
matches ``== "langchain"`` / ``startswith("langchain.")`` and never a bare
``startswith("langchain")``. The second test below pins that distinction by
asserting ``langchain_core`` *is* present in the very same subprocess the first
test requires ``langchain`` to be absent from: if the guard were widened by one
character it would start failing on code that is already correct.
"""

from __future__ import annotations

import subprocess
import sys

_LEAK_PROBE = (
    "import sys, zeroth.integrations.langgraph; "
    # Both packages have a Zeroth-side namesake (this very package is named
    # ...langgraph) and langchain has a core-dependency sibling named
    # langchain_core, so match the REAL top-level packages precisely rather
    # than by substring.
    "leaked = sorted(k for k in sys.modules "
    "if k in ('langgraph', 'langchain') "
    "or k.startswith('langgraph.') or k.startswith('langchain.')); "
)


def _probe(code: str) -> None:
    """Run one import probe in a clean interpreter, surfacing its stderr on failure."""
    try:
        subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(f"the package's eager imports regressed:\n{exc.stderr}") from exc


def test_importing_the_package_does_not_import_langgraph_or_langchain() -> None:
    _probe(
        _LEAK_PROBE + "assert not leaked, leaked; "
        "assert 'zeroth.integrations.langgraph._middleware' not in sys.modules"
    )


def test_the_guard_still_admits_the_core_langchain_dependency() -> None:
    """``langchain_core`` is eagerly imported on purpose and must stay admitted."""
    _probe(
        _LEAK_PROBE + "assert not leaked, leaked; "
        "core = [k for k in sys.modules "
        "if k == 'langchain_core' or k.startswith('langchain_core.')]; "
        "assert core, 'langchain_core is no longer imported eagerly'"
    )
