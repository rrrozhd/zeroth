"""The `# noqa: C901` suppressions ZER-24 introduced, pinned to an exact set.

ZER-24 relocated the gateway without changing a line of the moved bodies, but a
shim-preserving relocation cannot present to Git as a rename -- the shim keeps
the old path alive, so the new file is reported as an addition. Complexity gates
that read the staged file list therefore measured pre-existing complexity in code
this task did not otherwise touch, and the moved modules could not be committed
without either refactoring them or suppressing the rule. Suppression was chosen
deliberately; refactoring fail-closed gateway paths inside a pure move commit
would have mixed a behaviour-bearing change into a relocation.

This test exists because that decision set a precedent the repository had not
used before: prior to ZER-24 there were **zero** ``# noqa: C901`` comments in the
tree. Pinning the exact set keeps the precedent bounded. A new suppression is
then a deliberate edit to this list with a reviewer looking at it, rather than
something that accumulates quietly behind a gate nobody reads.

Every entry below is *inherited* complexity: the function was already above the
threshold before ZER-24 moved it. None of them is complexity this task authored.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
SEARCH_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "tests")

_SUPPRESSION = re.compile(r"#\s*noqa:[^\n]*\bC901\b")

# path -> the functions permitted to carry a C901 suppression.
EXPECTED_SUPPRESSIONS: dict[str, frozenset[str]] = {
    "src/zeroth/governance/langgraph_gateway/capabilities.py": frozenset({"_validated_level"}),
    "src/zeroth/governance/langgraph_gateway/events.py": frozenset({"_consume_sse_lines"}),
    "src/zeroth/service/langgraph_gateway/compatibility.py": frozenset({"detect"}),
    "src/zeroth/service/langgraph_gateway/context.py": frozenset({"decode"}),
    "src/zeroth/service/langgraph_gateway/proxy.py": frozenset({"handle_http"}),
    "src/zeroth/service/langgraph_gateway/routes.py": frozenset(
        {"handle", "transform_client_message"}
    ),
    "src/zeroth/service/langgraph_gateway/transport.py": frozenset({"_bridge_websocket"}),
    "tests/langgraph_gateway/conformance/harness.py": frozenset({"_normalize_generated"}),
    "tests/langgraph_gateway/test_http_proxy.py": frozenset(
        {"test_governed_pipeline_order_and_signed_claims_are_exact"}
    ),
}


def _actual_suppressions() -> dict[str, frozenset[str]]:
    """Map each file to the functions whose ``def`` line carries a C901 noqa."""
    found: dict[str, set[str]] = {}
    for root in SEARCH_ROOTS:
        for path in root.rglob("*.py"):
            if path == Path(__file__):
                # This registry quotes the marker in its own prose.
                continue
            text = path.read_text()
            if not _SUPPRESSION.search(text):
                continue
            lines = text.splitlines()
            names: set[str] = set()
            for node in ast.walk(ast.parse(text, filename=str(path))):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                # ``lineno`` is the ``def`` line itself; decorators sit above it.
                if _SUPPRESSION.search(lines[node.lineno - 1]):
                    names.add(node.name)
            found[path.relative_to(REPO_ROOT).as_posix()] = names
    return {path: frozenset(names) for path, names in found.items()}


def test_no_c901_suppression_exists_outside_the_pinned_set() -> None:
    """The suppression list is exactly what ZER-24 recorded -- no additions.

    A failure here is not necessarily a defect. It means someone added or moved a
    suppression, and the question to answer in review is whether the complexity
    is inherited (record it here) or newly authored (reduce it instead).
    """
    assert _actual_suppressions() == EXPECTED_SUPPRESSIONS


@pytest.mark.parametrize("path", sorted(EXPECTED_SUPPRESSIONS))
def test_every_suppression_records_why_it_is_inherited(path: str) -> None:
    """Each suppression carries the comment explaining it predates ZER-24.

    The bare ``# noqa: C901`` is the part a tool reads; the comment above it is
    the part a person reads, and it is what stops the next reader assuming this
    task wrote the complexity.
    """
    text = (REPO_ROOT / path).read_text()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _SUPPRESSION.search(line):
            continue
        preceding = "\n".join(lines[max(0, index - 4) : index])
        assert "predates ZER-24" in preceding, f"{path}:{index + 1} has no inheritance note"
