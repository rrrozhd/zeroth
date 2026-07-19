"""Enforce that importing the platform layer does not drag in higher domains.

The dependency matrix in :mod:`zeroth._architecture` is a *static* contract: it
checks which modules name which other modules. This module checks the runtime
consequence of those names — what actually lands in ``sys.modules`` when a
platform package is imported.

The two can disagree. A platform module may legally import another platform
module whose own package ``__init__`` eagerly pulls in half the codebase; the
static matrix sees a platform-to-platform edge while the interpreter loads
runtime, governance, and integration code. That inversion is what makes
extracting concrete adapters into their own packages impossible without
circular imports, because the extracted package is reached while the domain it
depends on is still initializing.

Every check runs in a subprocess. ``tests/conftest.py`` imports
``zeroth.core.service.bootstrap`` at collection time, so in-process assertions
about ``sys.modules`` would only ever describe the warm cache.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from zeroth._architecture import _canonical_domain


PLATFORM_ROOTS = (
    "zeroth.core.storage",
    "zeroth.core.config",
    "zeroth.platform.config",
    "zeroth.platform.storage",
    "zeroth.platform.primitives",
)

# Non-platform modules the platform layer is still permitted to load eagerly.
# Like the dependency exceptions in zeroth._architecture, this must match the
# real closure exactly, so nothing new slips in unnoticed.
#
# Empty since Task 11 moved the econ and http settings sections into
# ``zeroth.platform.config.models``: signature comparison became
# location-independent in 54378e7, so composing them as ``ZerothSettings``
# fields no longer forces their definitions to stay outside the platform
# layer. The legacy ``zeroth.core.econ.models`` and ``zeroth.core.http.models``
# paths republish the platform-owned classes, which points the import in the
# allowed direction.
PERMITTED_NON_PLATFORM: frozenset[str] = frozenset()

# Imported packages are free to print banners, warnings, or progress to stdout,
# so the payload is fenced by a sentinel rather than assumed to be the last line.
_SENTINEL = "---zeroth-import-probe---"

_PROBE = f"""
import json, sys
import {{root}}
print("{_SENTINEL}" + json.dumps([m for m in sys.modules if m.startswith("zeroth")]))
"""


def _modules_loaded_by(root: str) -> list[str]:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(root=root)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"importing {root} failed:\n{result.stderr}"

    payloads = [
        line.partition(_SENTINEL)[2] for line in result.stdout.splitlines() if _SENTINEL in line
    ]
    assert len(payloads) == 1, (
        f"expected exactly one probe payload from {root}, got {len(payloads)}."
        f"\nstdout:\n{result.stdout}"
    )
    return json.loads(payloads[0])


@pytest.mark.parametrize("root", PLATFORM_ROOTS)
def test_importing_a_platform_package_loads_only_platform_code(root: str) -> None:
    """A platform import must not execute runtime, governance, or econ modules.

    ``_canonical_domain`` returns ``None`` for package shells such as
    ``zeroth`` and ``zeroth.core`` that carry no domain; those are ignored.
    """
    loaded = _modules_loaded_by(root)

    offenders = sorted(
        {
            module
            for module in loaded
            if (domain := _canonical_domain(module)) is not None
            and domain != "platform"
            and module not in PERMITTED_NON_PLATFORM
        }
    )

    assert not offenders, (
        f"importing {root} eagerly loaded {len(offenders)} non-platform modules; "
        f"the platform layer must sit below every other domain:\n  " + "\n  ".join(offenders[:40])
    )


def test_the_permitted_non_platform_imports_are_all_still_real() -> None:
    """A permitted exception that no longer happens must be deleted, not left behind.

    Same discipline as ``TEMPORARY_EXCEPTIONS`` in :mod:`zeroth._architecture`:
    the allow-list documents present-day debt, so a stale entry would quietly
    license an import nobody has audited.
    """
    loaded: set[str] = set()
    for root in PLATFORM_ROOTS:
        loaded.update(_modules_loaded_by(root))

    stale = sorted(PERMITTED_NON_PLATFORM - loaded)

    assert not stale, f"permitted non-platform imports no longer occur: {stale}"


def test_reading_a_run_model_does_not_load_the_concrete_repository() -> None:
    """The run models must be usable without the SQL adapter behind them.

    ``zeroth.core.runs.__init__`` used to import the repository eagerly, so
    importing a run *model* also executed the concrete adapter and everything
    it depends on. Any module the adapter imports was therefore loaded while
    ``zeroth.core.runs`` was still initializing, which is what prevents that
    adapter from being extracted into its own package.
    """
    loaded = set(_modules_loaded_by("zeroth.core.runs.models"))

    assert "zeroth.core.runs.models" in loaded
    assert "zeroth.core.runs.repository" not in loaded


def test_importing_storage_does_not_load_the_run_domain() -> None:
    """The specific inversion that blocks extracting run persistence.

    ``zeroth.core.storage`` reaching ``zeroth.core.runs.repository`` means any
    module the repository imports is loaded while storage initializes — so a
    concrete persistence package cannot import the run models without closing
    a cycle.
    """
    loaded = set(_modules_loaded_by("zeroth.core.storage"))

    assert "zeroth.core.runs.models" not in loaded
    assert "zeroth.core.runs.repository" not in loaded
    assert "zeroth.core.agent_runtime" not in loaded
