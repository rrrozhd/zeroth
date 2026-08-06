"""The 14 legacy gateway import paths keep the public surface they had before ZER-24.

``fixtures/legacy_surface_manifest.json`` is an immutable capture taken at base
``e2f7ca1a``, before any module moved. It is the oracle for the compatibility promise:
every legacy path stays importable and keeps exactly the names it exported, whether it
declares ``__all__`` or relies on its star-import surface.

Identity against the canonical modules -- that each legacy name resolves to the *same
object* the canonical module exports -- is asserted separately, once the canonical
packages exist.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

_MANIFEST_PATH = Path(__file__).parent / "fixtures" / "legacy_surface_manifest.json"
_MANIFEST: dict[str, Any] = json.loads(_MANIFEST_PATH.read_text())
_MODULES: dict[str, Any] = _MANIFEST["modules"]

LEGACY_PATHS = tuple(_MODULES)


def _star_import_surface(module_name: str) -> list[str]:
    """The names ``from <module_name> import *`` actually binds.

    Executed rather than inferred. An earlier revision of this file approximated
    the surface with ``dir(module)``, which is the wrong oracle and hid a real
    regression: when ``__all__`` is absent, star-import reads the module
    *namespace* and never consults ``__dir__``, so a lazy shim that overrode
    ``__dir__`` alone exported almost nothing while this test still passed.
    """
    namespace: dict[str, Any] = {}
    exec(f"from {module_name} import *", namespace)  # noqa: S102 - the behaviour under test
    return sorted(name for name in namespace if not name.startswith("_"))


def test_manifest_covers_every_legacy_path() -> None:
    """The manifest pins the package plus its thirteen submodules."""
    assert len(LEGACY_PATHS) == 14
    assert "zeroth.core.langgraph_gateway" in LEGACY_PATHS


@pytest.mark.parametrize("module_name", LEGACY_PATHS)
def test_legacy_path_still_imports(module_name: str) -> None:
    """Every pre-ZER-24 import path remains importable."""
    assert importlib.import_module(module_name) is not None


@pytest.mark.parametrize("module_name", LEGACY_PATHS)
def test_legacy_path_preserves_its_star_import_surface(module_name: str) -> None:
    """No legacy path loses (or silently gains) a star-imported name."""
    assert _star_import_surface(module_name) == _MODULES[module_name]["names"]


@pytest.mark.parametrize("module_name", LEGACY_PATHS)
def test_every_legacy_path_declares_its_recorded_surface(module_name: str) -> None:
    """Every shim declares ``__all__``, including paths that did not before.

    A deliberate divergence from the pre-move modules, and the only way to keep
    the observable behaviour identical. A lazy shim resolves names through
    ``__getattr__``, which star-import does not consult; declaring ``__all__``
    is what makes ``from <path> import *`` bind the recorded names again. What
    callers observe -- the set of names a star-import yields -- is preserved
    exactly; only the unobservable question of whether the *original* module
    happened to declare ``__all__`` changes.
    """
    module = importlib.import_module(module_name)
    assert sorted(module.__all__) == _MODULES[module_name]["names"]


@pytest.mark.parametrize("module_name", LEGACY_PATHS)
def test_every_promised_name_is_reachable_by_attribute(module_name: str) -> None:
    """Each promised name resolves via attribute access, not just via ``dir()``.

    A shim that lists a name in ``__all__`` but cannot resolve it -- an export map
    typo, or a ``__getattr__`` that misses a case -- fails here rather than at a
    caller's import.
    """
    module = importlib.import_module(module_name)
    missing = [name for name in _MODULES[module_name]["names"] if not hasattr(module, name)]
    assert missing == []


# --------------------------------------------------------------------------
# Identity: a legacy name must be the *same object* the canonical module holds
# --------------------------------------------------------------------------

# Where each legacy path's names now live. The enforcement DTOs are the one
# split: the wire protocol went to ``integrations`` and the service class stayed
# in ``service``, so that path resolves against two canonical modules.
_CANONICAL = {
    "zeroth.core.langgraph_gateway": (
        "zeroth.contracts.langgraph_gateway.models",
        "zeroth.contracts.langgraph_gateway.inventory",
    ),
    "zeroth.core.langgraph_gateway.models": ("zeroth.contracts.langgraph_gateway.models",),
    "zeroth.core.langgraph_gateway.inventory": ("zeroth.contracts.langgraph_gateway.inventory",),
    "zeroth.core.langgraph_gateway.capabilities": (
        "zeroth.governance.langgraph_gateway.capabilities",
    ),
    "zeroth.core.langgraph_gateway.events": ("zeroth.governance.langgraph_gateway.events",),
    "zeroth.core.langgraph_gateway.admission": ("zeroth.service.langgraph_gateway.admission",),
    "zeroth.core.langgraph_gateway.compatibility": (
        "zeroth.service.langgraph_gateway.compatibility",
    ),
    "zeroth.core.langgraph_gateway.context": ("zeroth.service.langgraph_gateway.context",),
    "zeroth.core.langgraph_gateway.headers": ("zeroth.service.langgraph_gateway.headers",),
    "zeroth.core.langgraph_gateway.transport": ("zeroth.service.langgraph_gateway.transport",),
    "zeroth.core.langgraph_gateway.proxy": ("zeroth.service.langgraph_gateway.proxy",),
    "zeroth.core.langgraph_gateway.routes": ("zeroth.service.langgraph_gateway.routes",),
    "zeroth.core.langgraph_gateway.enforcement_store": (
        "zeroth.service.langgraph_gateway.enforcement_store",
    ),
    # Order matters: the wire-protocol owner is checked FIRST so the nine DTO
    # names are asserted against ``enforcement_protocol`` itself rather than
    # against the service module that merely re-exports them.
    "zeroth.core.langgraph_gateway.enforcement": (
        "zeroth.integrations.langgraph.enforcement_protocol",
        "zeroth.service.langgraph_gateway.enforcement",
    ),
}


def test_every_legacy_path_has_a_recorded_canonical_home() -> None:
    """The identity map covers all 14 paths, the package root included.

    The root was excluded in an earlier revision, which left the sixteen names it
    re-exports unchecked for identity -- the one surface most likely to be
    reached by an outside caller.
    """
    assert set(_CANONICAL) == set(LEGACY_PATHS)
    assert len(_CANONICAL) == 14


@pytest.mark.parametrize("module_name", sorted(_CANONICAL))
def test_legacy_names_are_the_canonical_objects(module_name: str) -> None:
    """A legacy name resolves to the *same object* the canonical module exports.

    Equality would not be enough. A shim that rebuilt an equal-looking class --
    or re-exported a stale copy left behind at the old path -- would satisfy the
    surface tests above while breaking ``isinstance`` for every caller that
    mixed the two import paths.
    """
    legacy = importlib.import_module(module_name)
    canonicals = [importlib.import_module(name) for name in _CANONICAL[module_name]]

    mismatched: list[str] = []
    for name in _MODULES[module_name]["names"]:
        legacy_object = getattr(legacy, name)
        for canonical in canonicals:
            if hasattr(canonical, name):
                if getattr(canonical, name) is not legacy_object:
                    mismatched.append(f"{name} ({canonical.__name__})")
                break
        else:  # pragma: no cover - a name no canonical module publishes
            mismatched.append(f"{name} (no canonical home)")
    assert mismatched == []
