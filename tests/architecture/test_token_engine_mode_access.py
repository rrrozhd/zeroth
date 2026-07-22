from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2] / "src" / "zeroth"
ALLOWED = {
    ROOT / "contracts" / "graph" / "models.py",
    ROOT / "contracts" / "graph" / "engine_mode.py",
}


def test_production_engine_decisions_use_the_canonical_helper() -> None:
    violations: list[str] = []
    for path in ROOT.rglob("*.py"):
        if path in ALLOWED:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "sequential_join_enabled":
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not violations, "raw structured-token mode reads: " + ", ".join(violations)
