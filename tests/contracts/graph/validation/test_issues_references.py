"""The shared primitives every graph validator builds on.

``issues`` owns constructing and accumulating ``ValidationIssue`` values;
``references`` owns the "does this string look like a usable ref" rules and
the graph-level reference checks. Everything else in the package depends on
these two, so they are extracted first.
"""

from __future__ import annotations

import pytest

from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation.references import (
    all_unique,
    is_ref_like,
    require_ref,
    validate_graph_refs,
    validate_ref_list,
)
from zeroth.contracts.graph.models import Graph
from zeroth.contracts.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("policy://safety", True),
        ("a", True),
        ("", False),
        ("   ", False),
        ("has space", False),
        ("  padded  ", True),
        ("tab\tinside", False),
    ],
)
def test_is_ref_like(value: str, expected: bool) -> None:
    assert is_ref_like(value) is expected


@pytest.mark.parametrize(
    ("values", "expected"),
    [([], True), (["a"], True), (["a", "b"], True), (["a", "a"], False)],
)
def test_all_unique(values: list[str], expected: bool) -> None:
    assert all_unique(values) is expected


def test_append_issue_builds_a_defaulted_issue() -> None:
    issues: list[ValidationIssue] = []
    append_issue(
        issues,
        severity=ValidationSeverity.ERROR,
        code=ValidationCode.EMPTY_GRAPH,
        message="boom",
        graph_id="g",
    )
    (issue,) = issues
    assert issue.severity is ValidationSeverity.ERROR
    assert issue.code is ValidationCode.EMPTY_GRAPH
    assert issue.message == "boom"
    assert issue.graph_id == "g"
    assert issue.node_id is None
    assert issue.edge_id is None
    assert issue.path == ()
    assert issue.details == {}


def test_append_issue_copies_details() -> None:
    """The caller's dict must not alias the frozen issue's details."""
    issues: list[ValidationIssue] = []
    details = {"ref": "x"}
    append_issue(
        issues,
        severity=ValidationSeverity.WARNING,
        code=ValidationCode.INVALID_POLICY_REF,
        message="m",
        graph_id="g",
        node_id="n",
        edge_id="e",
        path=("a", "b"),
        details=details,
    )
    details["ref"] = "mutated"
    assert issues[0].details == {"ref": "x"}
    assert issues[0].path == ("a", "b")


@pytest.mark.parametrize("value", [None, "", "   ", "has space"])
def test_require_ref_records_missing_or_invalid(value: str | None) -> None:
    issues: list[ValidationIssue] = []
    require_ref(
        issues,
        graph_id="g",
        node_id="n",
        code=ValidationCode.MISSING_CONTRACT_REF,
        message="input contract ref is required",
        value=value,
        path=("nodes", "n", "input_contract_ref"),
    )
    (issue,) = issues
    assert issue.code is ValidationCode.MISSING_CONTRACT_REF
    assert issue.details == {"ref": value}


def test_require_ref_accepts_a_usable_ref() -> None:
    issues: list[ValidationIssue] = []
    require_ref(
        issues,
        graph_id="g",
        node_id="n",
        code=ValidationCode.MISSING_CONTRACT_REF,
        message="m",
        value="contract://in",
        path=("nodes", "n", "input_contract_ref"),
    )
    assert issues == []


def test_validate_ref_list_indexes_the_offending_entry() -> None:
    issues: list[ValidationIssue] = []
    validate_ref_list(
        issues,
        graph_id="g",
        refs=["ok://a", "  ", "ok://b", ""],
        code=ValidationCode.INVALID_POLICY_REF,
        message="invalid node policy reference",
        path=("nodes", "n", "policy_bindings"),
        node_id="n",
    )
    assert [issue.path for issue in issues] == [
        ("nodes", "n", "policy_bindings", "1"),
        ("nodes", "n", "policy_bindings", "3"),
    ]
    assert {issue.node_id for issue in issues} == {"n"}


def test_validate_graph_refs_checks_graph_policy_bindings() -> None:
    graph = Graph(graph_id="g", name="G", policy_bindings=["ok://a", "   "])
    issues: list[ValidationIssue] = []
    validate_graph_refs(graph, issues)
    (issue,) = issues
    assert issue.code is ValidationCode.INVALID_POLICY_REF
    assert issue.message == "invalid policy reference: '   '"
    assert issue.path == ("policy_bindings",)
    assert issue.details == {"ref": "   "}
