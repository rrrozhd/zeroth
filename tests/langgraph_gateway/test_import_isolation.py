"""Import-time isolation for the relocated gateway, proven in fresh interpreters.

These facts are about what is in ``sys.modules`` at a given moment, so they
cannot be tested in-process: this suite's own conftest and its sibling tests
have already imported most of the tree, and an assertion about absence would
pass or fail depending on test ordering. Each case runs in its own subprocess.

ZER-24 relocated the gateway behind lazy ``zeroth.core.langgraph_gateway``
shims, and this module proved each shim stayed lazy and still resolved. ZER-25
deleted the shims, so that half is gone with them. What survives is the fact
they existed to protect: each canonical gateway package imports cold, and
importing one drags in neither its siblings' service machinery nor anything
retired.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

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


@pytest.mark.parametrize("package", CANONICAL_PACKAGES)
def test_canonical_packages_import_cold(package: str) -> None:
    """A canonical package imports without dragging in a retired module."""
    result = _run(f"""
        import sys
        import {package}  # noqa: F401
        retired = [m for m in sys.modules if m.startswith(("zeroth.core", "zeroth.econ_plane"))]
        print(retired)
    """)
    assert result == "[]"


#: Every submodule each canonical package owns, so the relocation is proven
#: complete rather than proven for whichever modules happened to be imported.
CANONICAL_SUBMODULES = [
    ("zeroth.contracts.langgraph_gateway", ("inventory", "models")),
    ("zeroth.governance.langgraph_gateway", ("capabilities", "events")),
    (
        "zeroth.service.langgraph_gateway",
        (
            "admission",
            "compatibility",
            "context",
            "enforcement",
            "enforcement_store",
            "headers",
            "proxy",
            "routes",
            "transport",
        ),
    ),
]


@pytest.mark.parametrize("package,submodules", CANONICAL_SUBMODULES, ids=lambda v: str(v)[:40])
def test_canonical_packages_resolve_every_submodule(
    package: str, submodules: tuple[str, ...]
) -> None:
    """Importing a package is not enough; the modules it owns must resolve.

    This keeps the second half of the removed shim pair. Laziness alone was
    always satisfiable by a module that resolved nothing at all, so each
    canonical package is asserted to actually stand its submodules up -- and the
    list is exhaustive, because the test it replaces was pinned to a full
    fourteen paths after an earlier revision silently covered only nine.
    """
    result = _run(f"""
        import importlib
        for name in {submodules!r}:
            importlib.import_module(f"{package}.{{name}}")
        print("ok")
    """)
    assert result == "ok"


def test_the_contracts_package_does_not_load_the_service_slice() -> None:
    """The gateway's data shapes stay usable without its request machinery.

    This is the isolation the three-package split exists for: a consumer reading
    a gateway model must not pay for the proxy, the transport, or the service
    application they belong to.
    """
    result = _run("""
        import sys
        import zeroth.contracts.langgraph_gateway  # noqa: F401
        eager = [m for m in sys.modules if m.startswith("zeroth.service")]
        print(eager)
    """)
    assert result == "[]"
