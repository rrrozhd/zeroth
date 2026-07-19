"""Subprocess cold-import guard for the canonical service packages.

``tests/conftest.py`` imports ``zeroth.core.service.bootstrap`` at collection
time, so every in-process test runs with ``zeroth.core`` warm and a circular
import between the canonical service packages and ``zeroth.core`` is
structurally invisible to the suite. A library consumer has no such warm
cache, so both import directions must work from a cold interpreter.

Two subprocesses instead of one per module: partial-initialization cycles are
order-dependent, so what matters is entering the graph cold from each side.
The first import in each subprocess is the genuinely cold probe; the rest
assert the closure stays consistent once one side is warm.

CANONICAL_SERVICE_MODULES grows as Task 10 relocates modules; every module
listed here must remain importable through both its canonical and its legacy
path.
"""

from __future__ import annotations

import subprocess
import sys

# (canonical module, legacy module) pairs, in relocation order.
RELOCATED_SERVICE_MODULES = [
    ("zeroth.service.api.studio_schemas", "zeroth.core.service.studio_schemas"),
    ("zeroth.service.bootstrap.configuration", "zeroth.core.service.bootstrap"),
    ("zeroth.service.bootstrap.migrations", "zeroth.core.service.bootstrap"),
]


def _import_all(module_names: list[str]) -> subprocess.CompletedProcess[str]:
    code = "\n".join(f"import {name}" for name in module_names)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )


def test_canonical_service_modules_import_in_a_cold_interpreter() -> None:
    """Canonical-first: no relocated module may need ``zeroth.core`` pre-warmed."""
    canonical = [pair[0] for pair in RELOCATED_SERVICE_MODULES]
    result = _import_all(canonical + [pair[1] for pair in RELOCATED_SERVICE_MODULES])
    assert result.returncode == 0, f"canonical-first cold import failed:\n{result.stderr}"


def test_legacy_service_modules_import_in_a_cold_interpreter() -> None:
    """Legacy-first: the shims must not re-enter a partially initialized canonical module."""
    legacy = [pair[1] for pair in RELOCATED_SERVICE_MODULES]
    result = _import_all(legacy + [pair[0] for pair in RELOCATED_SERVICE_MODULES])
    assert result.returncode == 0, f"legacy-first cold import failed:\n{result.stderr}"
