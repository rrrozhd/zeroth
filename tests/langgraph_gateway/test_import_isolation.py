"""Import-time isolation for the relocated gateway, proven in fresh interpreters.

Both facts here are about what is in ``sys.modules`` at a given moment, so they
cannot be tested in-process: this suite's own conftest and its sibling tests
have already imported most of the tree, and an assertion about absence would
pass or fail depending on test ordering. Each case therefore runs in its own
subprocess with a cold interpreter.

* **Laziness.** Importing a legacy shim must not pull in the canonical module.
  This is what keeps the compatibility layer free: the shims exist for outside
  callers, and importing one should cost nothing until a name is actually read.
* **Cold canonical imports.** Each canonical package must import without
  ``zeroth.core`` -- the inversion that made the canonical packages
  uncold-importable before the backend refactor.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

# Every one of the 14 legacy paths, each with the canonical module its import
# must NOT pull in. The package root resolves against ``contracts`` because that
# is where its sixteen re-exported names live; ``enforcement`` is paired with the
# service slice, and its wire-protocol half is covered by the identity suite in
# ``test_legacy_surface_parity.py``.
LAZY_SHIMS = [
    ("zeroth.core.langgraph_gateway", "zeroth.contracts.langgraph_gateway.models"),
    ("zeroth.core.langgraph_gateway.models", "zeroth.contracts.langgraph_gateway.models"),
    ("zeroth.core.langgraph_gateway.inventory", "zeroth.contracts.langgraph_gateway.inventory"),
    (
        "zeroth.core.langgraph_gateway.capabilities",
        "zeroth.governance.langgraph_gateway.capabilities",
    ),
    ("zeroth.core.langgraph_gateway.events", "zeroth.governance.langgraph_gateway.events"),
    ("zeroth.core.langgraph_gateway.admission", "zeroth.service.langgraph_gateway.admission"),
    (
        "zeroth.core.langgraph_gateway.compatibility",
        "zeroth.service.langgraph_gateway.compatibility",
    ),
    ("zeroth.core.langgraph_gateway.context", "zeroth.service.langgraph_gateway.context"),
    ("zeroth.core.langgraph_gateway.headers", "zeroth.service.langgraph_gateway.headers"),
    ("zeroth.core.langgraph_gateway.transport", "zeroth.service.langgraph_gateway.transport"),
    ("zeroth.core.langgraph_gateway.enforcement", "zeroth.service.langgraph_gateway.enforcement"),
    (
        "zeroth.core.langgraph_gateway.enforcement_store",
        "zeroth.service.langgraph_gateway.enforcement_store",
    ),
    ("zeroth.core.langgraph_gateway.proxy", "zeroth.service.langgraph_gateway.proxy"),
    ("zeroth.core.langgraph_gateway.routes", "zeroth.service.langgraph_gateway.routes"),
]


def test_the_lazy_matrix_covers_every_legacy_path() -> None:
    """All 14 paths, not a convenient subset.

    Pinned because an earlier revision covered only nine and the five it omitted
    were lazy by luck rather than by proof.
    """
    assert len(LAZY_SHIMS) == 14


CANONICAL_PACKAGES = [
    "zeroth.contracts.langgraph_gateway",
    "zeroth.governance.langgraph_gateway",
    "zeroth.service.langgraph_gateway",
]


def _run(script: str) -> str:
    """Execute ``script`` in a cold interpreter and return its stdout."""
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


@pytest.mark.parametrize("legacy,canonical", LAZY_SHIMS, ids=lambda v: v.rsplit(".", 1)[-1])
def test_importing_a_shim_is_lazy(legacy: str, canonical: str) -> None:
    """Importing the legacy path leaves the canonical module unloaded."""
    loaded = _run(f"""
        import sys
        import {legacy}  # noqa: F401
        print({canonical!r} in sys.modules)
    """)
    assert loaded == "False"


@pytest.mark.parametrize("legacy,canonical", LAZY_SHIMS, ids=lambda v: v.rsplit(".", 1)[-1])
def test_reading_a_name_then_loads_the_canonical_module(legacy: str, canonical: str) -> None:
    """Touching a recorded export resolves it, and yields the canonical object.

    The pair matters: laziness alone would be satisfied by a shim that never
    resolved anything at all.
    """
    # ``enforcement_store`` cannot be the first gateway module a cold interpreter
    # loads: it imports ``enforcement``, which late-imports it back. That cycle
    # predates ZER-24 -- importing the legacy path first fails identically at base
    # ``e2f7ca1a`` -- so this test enters through ``enforcement`` the way every
    # real caller does, rather than pretending the relocation caused it.
    prerequisite = (
        f"import {canonical.rsplit('.', 1)[0]}.enforcement"
        if legacy.endswith("enforcement_store")
        else "pass"
    )
    result = _run(f"""
        import importlib
        import sys
        {prerequisite}
        shim = importlib.import_module({legacy!r})
        # Pick a name the shim actually delegates. Some recorded names -- ``Any``,
        # ``annotations`` -- are incidental imports the shim happens to hold as
        # real globals, so reading one never reaches ``__getattr__`` and would
        # make this assertion vacuous.
        own = vars(shim)
        name = next(n for n in sorted(shim.__dir__()) if n not in own)
        value = getattr(shim, name)
        canonical = sys.modules[{canonical!r}]
        print({canonical!r} in sys.modules, getattr(canonical, name, None) is value)
    """)
    assert result == "True True"


@pytest.mark.parametrize("package", CANONICAL_PACKAGES)
def test_canonical_packages_import_cold(package: str) -> None:
    """A canonical package imports without dragging in the legacy tree."""
    result = _run(f"""
        import sys
        import {package}  # noqa: F401
        legacy = [m for m in sys.modules if m.startswith("zeroth.core.langgraph_gateway")]
        print(legacy)
    """)
    assert result == "[]"
