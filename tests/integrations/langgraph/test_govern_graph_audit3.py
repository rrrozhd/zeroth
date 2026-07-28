"""Audit-3 (F7): BaseCallbackManager governance normalization.

These need no ``langgraph`` (they exercise the pure callback-merge logic), so they
run in the default suite rather than under the ``langgraph_conformance`` marker.
"""

from __future__ import annotations

from langchain_core.callbacks import BaseCallbackManager

from zeroth.integrations.langgraph._callbacks import merge_governance_callbacks
from zeroth.integrations.langgraph._handler import ZerothGovernanceCallbackHandler


def _governance(handlers: list) -> list:
    return [h for h in handlers if isinstance(h, ZerothGovernanceCallbackHandler)]


def test_f7_divergent_governance_instances_are_normalized_to_one() -> None:
    """Different governance instances across the two lists collapse to one identity."""
    a = ZerothGovernanceCallbackHandler()
    b = ZerothGovernanceCallbackHandler()
    assert a is not b
    manager = BaseCallbackManager(handlers=[a], inheritable_handlers=[b])
    new = ZerothGovernanceCallbackHandler()

    result = merge_governance_callbacks({"callbacks": manager}, new)["callbacks"]

    gov_handlers = _governance(result.handlers)
    gov_inheritable = _governance(result.inheritable_handlers)
    assert len(gov_handlers) == 1
    assert len(gov_inheritable) == 1
    # The SAME canonical identity lives in both lists (not two divergent ones).
    assert gov_handlers[0] is gov_inheritable[0]
    # The caller's own manager is left untouched.
    assert _governance(manager.handlers) == [a]
    assert _governance(manager.inheritable_handlers) == [b]


def test_f7_preexisting_duplicate_governance_is_collapsed() -> None:
    """Pre-existing duplicate governance entries collapse to a single instance."""
    a = ZerothGovernanceCallbackHandler()
    manager = BaseCallbackManager(handlers=[a, a], inheritable_handlers=[a, a])
    new = ZerothGovernanceCallbackHandler()

    result = merge_governance_callbacks({"callbacks": manager}, new)["callbacks"]

    assert len(_governance(result.handlers)) == 1
    assert len(_governance(result.inheritable_handlers)) == 1


def test_f7_non_governance_handlers_are_preserved_and_ordered() -> None:
    """Normalization strips only governance handlers; user callbacks survive."""
    user_a = _PlainHandler()
    user_b = _PlainHandler()
    gov = ZerothGovernanceCallbackHandler()
    manager = BaseCallbackManager(handlers=[user_a, gov, user_b], inheritable_handlers=[])
    new = ZerothGovernanceCallbackHandler()

    result = merge_governance_callbacks({"callbacks": manager}, new)["callbacks"]

    non_gov = [h for h in result.handlers if not isinstance(h, ZerothGovernanceCallbackHandler)]
    assert non_gov == [user_a, user_b]
    assert len(_governance(result.handlers)) == 1
    # Governance also propagated into the (empty) inheritable list.
    assert len(_governance(result.inheritable_handlers)) == 1


class _PlainHandler:
    """A minimal non-governance callback with default (identity) equality."""
