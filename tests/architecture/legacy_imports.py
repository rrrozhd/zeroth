"""Locate executable imports of the retired ``zeroth.core`` / ``zeroth.econ_plane`` trees.

Shared by the removal guard and the baseline it ratchets against. The scan is
AST-based on purpose: a substring search cannot tell an import from a docstring
that names the old path, and this repository is full of the latter — migration
guides, changelog entries, and the immutable legacy-surface fixture all mention
``zeroth.core`` in prose that must survive the removal untouched.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]

#: Import roots retired by ZER-25. A module matches when it is one of these or a
#: descendant of one — ``zeroth.corex`` is not a match.
LEGACY_ROOTS = ("zeroth.core", "zeroth.econ_plane")

#: Trees whose Python files must not import a legacy root. Mirrors the scope the
#: task specifies; ``frontend`` and ``scripts`` hold no Python importing zeroth.
SCAN_TREES = ("src/zeroth", "tests", "apps/vendor_dd", "examples")


def is_legacy_module(name: str | None) -> bool:
    """Whether ``name`` names a retired module or one of its descendants."""
    if not name:
        return False
    return any(name == root or name.startswith(f"{root}.") for root in LEGACY_ROOTS)


def _resolve(node: ast.ImportFrom, module_path: Path) -> str | None:
    """Absolute module name for an ``ImportFrom``, resolving relative levels.

    Relative levels are resolved only for files under ``src``, because only there
    does a relative import share a package root with ``zeroth``. Measured: the
    other scanned trees hold 9 relative imports, every one a level-1 sibling of a
    local test helper (``._graphs``, ``._causal``, ``.harness``, ``.cases``), and
    no relative import rooted in ``tests`` can reach ``zeroth.core`` at all.
    """
    if not node.level:
        return node.module
    # ``a/b/c.py`` and ``a/b/__init__.py`` both sit *in* package ``a.b``, so one
    # rule covers both: drop the filename, then climb one package per extra level.
    package = module_path.relative_to(REPO_ROOT / "src").with_suffix("").parts[:-1]
    base = package[: len(package) - node.level + 1]
    return ".".join((*base, node.module)) if node.module else ".".join(base)


def legacy_imports_in(path: Path) -> list[tuple[int, str]]:
    """Every ``(line, module)`` pair in ``path`` that imports a retired module."""
    # Deliberately not caught: a file this cannot parse must fail loudly rather
    # than be reported as clean. Swallowing the error turns "we could not look"
    # into "we looked and found nothing", which is how an offender slips through
    # the guard. (Running the scan under an older interpreter than the project's
    # did exactly that during ZER-25 and silently dropped a real offender.)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                (node.lineno, alias.name) for alias in node.names if is_legacy_module(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            resolved = (
                _resolve(node, path) if path.is_relative_to(REPO_ROOT / "src") else node.module
            )
            if is_legacy_module(resolved):
                found.append((node.lineno, resolved or ""))
    return found


def scan_repository() -> dict[str, list[tuple[int, str]]]:
    """Repository-relative path -> legacy imports, for every scanned tree."""
    offenders: dict[str, list[tuple[int, str]]] = {}
    for tree in SCAN_TREES:
        root = REPO_ROOT / tree
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            hits = legacy_imports_in(path)
            if hits:
                offenders[path.relative_to(REPO_ROOT).as_posix()] = hits
    return offenders
