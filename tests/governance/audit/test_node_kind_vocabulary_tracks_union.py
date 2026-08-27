"""The audit ``node_kind`` vocabulary must track the graph node union.

``METADATA_VOCABULARIES["node_kind"]`` is a hand-written frozenset that has to
stay in step with the ``node_type`` discriminators of ``contracts.graph.Node``.
Nothing enforced that before, so adding a node kind and forgetting the
vocabulary would not fail anything -- the audit layer would simply start
rejecting or mislabelling that kind's metadata at run time, which is the
quietest possible way for an audit trail to become wrong.

This asserts the property (the two sets agree) rather than a copy of it, so it
cannot be satisfied by updating a second hand-written list.
"""

from __future__ import annotations

import typing

from zeroth.contracts.graph.models import Node
from zeroth.governance.audit.capture_vocabulary import METADATA_VOCABULARIES


def _union_node_types() -> set[str]:
    """Every ``node_type`` literal in the discriminated ``Node`` union."""
    annotated_args = typing.get_args(Node)
    union = annotated_args[0]
    kinds: set[str] = set()
    for member in typing.get_args(union):
        literal = member.model_fields["node_type"].annotation
        kinds.update(typing.get_args(literal))
    return kinds


def test_every_node_kind_in_the_union_is_an_allowed_audit_value() -> None:
    missing = _union_node_types() - set(METADATA_VOCABULARIES["node_kind"])
    assert missing == set(), (
        f"node kinds in the graph union but not in the audit vocabulary: {sorted(missing)}"
    )


def test_the_vocabulary_names_no_kind_the_union_does_not_have() -> None:
    """A stale entry means a kind was removed and the vocabulary kept a ghost."""
    extra = set(METADATA_VOCABULARIES["node_kind"]) - _union_node_types()
    assert extra == set(), (
        f"audit vocabulary names node kinds the graph union does not: {sorted(extra)}"
    )


def test_the_guard_would_notice_a_missing_kind() -> None:
    """Proof the comparison is real: remove a kind and the check must fail."""
    union_kinds = _union_node_types()
    assert "mcp_tool" in union_kinds
    pruned = set(METADATA_VOCABULARIES["node_kind"]) - {"mcp_tool"}
    assert union_kinds - pruned == {"mcp_tool"}
