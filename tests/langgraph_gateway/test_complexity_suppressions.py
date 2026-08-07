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
import os
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
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


def _tracked_python_files(repo_root: Path) -> tuple[Path, ...]:
    """Return every Python file Git tracks, including tools outside package roots."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return tuple(
        repo_root / os.fsdecode(relative) for relative in result.stdout.split(b"\0") if relative
    )


def _actual_suppressions(
    *,
    repo_root: Path = REPO_ROOT,
    paths: Iterable[Path] | None = None,
) -> dict[str, frozenset[str]]:
    """Map each file to the functions whose ``def`` line carries a C901 noqa."""
    found: dict[str, set[str]] = {}
    for path in paths if paths is not None else _tracked_python_files(repo_root):
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
        if names:
            found[path.relative_to(repo_root).as_posix()] = names
    return {path: frozenset(names) for path, names in found.items()}


def test_no_c901_suppression_exists_outside_the_pinned_set() -> None:
    """The suppression list is exactly what ZER-24 recorded -- no additions.

    A failure here is not necessarily a defect. It means someone added or moved a
    suppression, and the question to answer in review is whether the complexity
    is inherited (record it here) or newly authored (reduce it instead).
    """
    assert _actual_suppressions() == EXPECTED_SUPPRESSIONS


def test_scanner_accepts_tracked_python_files_outside_src_and_tests(tmp_path: Path) -> None:
    """A repository-level registry must inspect Python tools as well as packages."""
    script = tmp_path / "scripts" / "tool.py"
    script.parent.mkdir()
    script.write_text("def tool(value):  # noqa: C901\n    return value\n")

    assert _actual_suppressions(repo_root=tmp_path, paths=[script]) == {
        "scripts/tool.py": frozenset({"tool"})
    }


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
