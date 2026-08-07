"""Reproduce the counts behind the omitted assertion-free-test rule.

The vacuity guard in ``tests/architecture/test_legacy_surface_removed.py``
cites two numbers as its reason for not rejecting assertion-free test bodies.
This script regenerates both from the current test tree.

Run from the repository root with::

    uv run python scripts/count_assertion_free_tests.py
"""

from __future__ import annotations

import ast
from pathlib import Path

DELEGATES = ("_assert_implements",)


def asserts_somehow(node: ast.AST) -> bool:
    """Whether a test asserts in any form this static analysis recognizes."""
    for inner in ast.walk(node):
        if isinstance(inner, ast.Assert):
            return True
        if isinstance(inner, ast.Call):
            rendered = ast.unparse(inner.func)
            if rendered.endswith(("pytest.raises", "pytest.fail", "pytest.warns")):
                return True
            if rendered.rsplit(".", 1)[-1].startswith("assert_"):
                return True
            if any(rendered.endswith(name) for name in DELEGATES):
                return True
    return False


def main() -> None:
    """Print the bare-assert and unexplained assertion-free test counts."""
    bare = unexplained = 0
    for path in sorted(Path("tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            if not any(isinstance(inner, ast.Assert) for inner in ast.walk(node)):
                bare += 1
            if not asserts_somehow(node):
                unexplained += 1
    print(f"no bare `assert` statement: {bare}")
    print(f"unexplained after crediting the tolerant forms: {unexplained}")


if __name__ == "__main__":
    main()
