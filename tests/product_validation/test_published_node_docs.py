from __future__ import annotations

import re
from pathlib import Path

from zeroth.service.api.studio_api import _NODE_TYPES


ROOT = Path(__file__).resolve().parents[2]


def _documented_node_labels() -> set[str]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("### Node Types", 1)[1].split("### Contracts", 1)[0]
    return set(re.findall(r"^- \*\*(.+?)\*\*", section, flags=re.MULTILINE))


def test_readme_node_types_match_the_executable_studio_registry() -> None:
    assert _documented_node_labels() == {node.label for node in _NODE_TYPES}


def test_condition_docs_distinguish_studio_if_nodes_from_legacy_edge_conditions() -> None:
    conditions = (ROOT / "docs/concepts/conditions.md").read_text(encoding="utf-8")
    graph = (ROOT / "docs/concepts/graph.md").read_text(encoding="utf-8")

    assert "Studio" in conditions
    assert "If node" in conditions
    assert "legacy" in conditions.lower()
    assert "If and Loop nodes" in graph
    assert "legacy" in graph.lower()
