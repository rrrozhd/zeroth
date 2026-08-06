"""The legacy gateway directory holds shims, and nothing reaches it any more.

Two structural facts ZER-24 has to keep true after the relocation, both checked
by reading the source rather than by importing it:

* the legacy package defines no production code -- only the machinery a lazy
  compatibility shim needs;
* no module under ``src/`` imports the legacy paths, so the shims exist purely
  for callers outside this repository.

An import-based check could not prove either one: a shim resolves every name to
the canonical object, so at runtime a shim and the real module look alike. The
AST is where the difference is visible.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).parents[2] / "src"
LEGACY_DIR = SOURCE_ROOT / "zeroth" / "core" / "langgraph_gateway"
LEGACY_PREFIX = "zeroth.core.langgraph_gateway"

# The only names a lazy shim needs at module scope. ``_EXPORTS`` is the recorded
# surface, ``__all__`` republishes it, and the two dunder functions are how the
# surface stays reachable without importing the canonical module eagerly.
_ALLOWED_ASSIGNMENTS = frozenset({"_EXPORTS", "__all__"})
_ALLOWED_FUNCTIONS = frozenset({"__getattr__", "__dir__"})

LEGACY_MODULES = sorted(LEGACY_DIR.glob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def test_the_legacy_directory_still_has_every_path_it_promised() -> None:
    """Exactly the paths the pre-move manifest recorded, by name.

    Guards the compatibility promise from the other direction -- a shim that was
    deleted rather than emptied would pass every test below. Asserted as a set
    of module names rather than a count: a count alone is satisfied by a deleted
    shim plus any unrelated file, which is the failure this is meant to catch.
    """
    manifest = json.loads(
        (Path(__file__).parent / "fixtures" / "legacy_surface_manifest.json").read_text()
    )["modules"]
    recorded = {
        name.rsplit(".", 1)[-1] if name != "zeroth.core.langgraph_gateway" else "__init__"
        for name in manifest
    }
    assert {path.stem for path in LEGACY_MODULES} == recorded
    assert len(LEGACY_MODULES) == 14


@pytest.mark.parametrize("path", LEGACY_MODULES, ids=lambda p: p.stem)
def test_the_legacy_directory_holds_only_shim_machinery(path: Path) -> None:
    """No class, and no function beyond ``__getattr__``/``__dir__``.

    This is the requirement the relocation exists to satisfy: the production
    definitions must live in the canonical packages, not here.
    """
    offenders: list[str] = []
    for node in _tree(path).body:
        if isinstance(node, ast.ClassDef):
            offenders.append(f"class {node.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name not in _ALLOWED_FUNCTIONS:
                offenders.append(f"def {node.name}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in _ALLOWED_ASSIGNMENTS:
                    offenders.append(f"assignment {target.id}")
    assert offenders == [], f"{path.name} still defines production code: {offenders}"


@pytest.mark.parametrize("path", LEGACY_MODULES, ids=lambda p: p.stem)
def test_every_legacy_module_resolves_lazily(path: Path) -> None:
    """The canonical import sits inside a function, never at module scope.

    A module-scope import would make the shim eager, and because Python imports
    a package before its submodules, one eager shim would drag the canonical
    tree onto the import path of every other shim beside it.
    """
    for node in _tree(path).body:
        if isinstance(node, ast.Import | ast.ImportFrom):
            module = getattr(node, "module", None) or ""
            names = [alias.name for alias in node.names]
            eager = any(n.startswith("zeroth.") for n in names) or module.startswith("zeroth.")
            # ``TYPE_CHECKING`` blocks are ast.If, not bare imports, so a
            # canonical import reached here is genuinely executed at import time.
            assert not eager, f"{path.name} imports {module or names} at module scope"


def _iter_source_modules() -> list[Path]:
    return sorted(p for p in SOURCE_ROOT.rglob("*.py") if not p.is_relative_to(LEGACY_DIR))


@pytest.mark.parametrize("path", _iter_source_modules(), ids=lambda p: p.stem)
def test_no_legacy_gateway_imports_remain_under_src(path: Path) -> None:
    """Nothing in the tree imports the legacy gateway paths.

    ``_architecture.py`` is exempt: it is the dependency policy itself, and it
    names the legacy prefixes on purpose so the classification survives.
    """
    if path.name == "_architecture.py":
        pytest.skip("the policy table names the legacy prefixes deliberately")
    offenders: list[str] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(LEGACY_PREFIX):
            offenders.append(f"line {node.lineno}: from {node.module}")
        elif isinstance(node, ast.Import):
            offenders.extend(
                f"line {node.lineno}: import {alias.name}"
                for alias in node.names
                if alias.name.startswith(LEGACY_PREFIX)
            )
    assert offenders == [], f"{path} still imports the legacy gateway: {offenders}"
