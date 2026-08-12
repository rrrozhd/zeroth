"""Task 9 transactions must not expose or extract raw database connections."""

from __future__ import annotations

import ast
from pathlib import Path


def test_retention_coordination_does_not_extract_private_connection() -> None:
    root = Path(__file__).resolve().parents[2]
    paths = (
        root / "src/zeroth/governance/retention/coordination.py",
        root / "src/zeroth/governance/retention/claims.py",
        root / "src/zeroth/governance/retention/erasure_service.py",
        root / "src/zeroth/governance/retention/legal_hold_repository.py",
    )
    violations: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and (
                "BoundStructuredTable__connection" in node.attr
                or node.attr in {"raw_connection", "connection"}
                and isinstance(node.value, ast.Name)
                and node.value.id in {"coordination", "bound", "table"}
            ):
                violations.append(f"{path.relative_to(root)}:{node.lineno}:{node.attr}")
    assert violations == []
