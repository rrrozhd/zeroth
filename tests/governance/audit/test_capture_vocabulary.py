"""Pin each transcribed capture vocabulary against the enum it mirrors.

``METADATA_VOCABULARIES`` decides which values the capture projection retains as
readable text. Several of its closed sets are hand-transcribed copies of enums
that live outside ``zeroth.governance`` -- ``SandboxStrictnessMode`` sits in
``integrations`` and ``GovernanceLevel`` in the gateway package, neither of which
``governance`` is allowed to import (``zeroth/_architecture.py``). A transcription
that drifts from its source fails *open* in the direction that matters: a new enum
member is silently summarized away, so a real decision stops being retained and
the audit row quietly loses evidence it is supposed to keep.

Tests are not bound by the layering rule, so this module imports both sides and
asserts they still agree. It is the safety net ``capture_vocabulary`` claims to
have.
"""

from __future__ import annotations

from zeroth.contracts.governed.models.approval import ApprovalDecisionType
from zeroth.contracts.langgraph_gateway.models import GovernanceLevel, RouteDisposition
from zeroth.governance.audit.capture_vocabulary import METADATA_VOCABULARIES
from zeroth.governance.policy.models import PolicyDecision
from zeroth.integrations.execution.sandbox import SandboxStrictnessMode


def _values(enum: type) -> set[str]:
    """Return the string values of ``enum`` as a plain set."""
    return {str(member.value) for member in enum}


def test_the_decision_vocabulary_covers_both_enums_that_mint_a_decision() -> None:
    missing = (_values(PolicyDecision) | _values(ApprovalDecisionType)) - METADATA_VOCABULARIES[
        "decision"
    ]
    assert not missing, f"decision vocabulary drifted; unretained members: {sorted(missing)}"


def test_the_governance_level_vocabulary_covers_the_gateway_enum() -> None:
    missing = _values(GovernanceLevel) - METADATA_VOCABULARIES["governance_level"]
    assert not missing, f"governance_level vocabulary drifted; unretained: {sorted(missing)}"


def test_the_sandbox_strictness_vocabulary_covers_the_execution_enum() -> None:
    missing = _values(SandboxStrictnessMode) - METADATA_VOCABULARIES["sandbox_strictness_mode"]
    assert not missing, f"sandbox_strictness_mode vocabulary drifted; unretained: {sorted(missing)}"


def test_the_disposition_vocabulary_covers_every_route_disposition() -> None:
    missing = _values(RouteDisposition) - METADATA_VOCABULARIES["disposition"]
    assert not missing, f"disposition vocabulary drifted; unretained: {sorted(missing)}"


def test_the_disposition_vocabulary_also_carries_the_cache_outcomes() -> None:
    # These are not enum members anywhere: the agent serializer promotes them and
    # the econ waste analyzer reads them back, so a drift test over RouteDisposition
    # alone would not notice them disappearing.
    assert {"cache_hit", "cache_miss"} <= METADATA_VOCABULARIES["disposition"]


def test_every_vocabulary_term_is_label_shaped_so_it_survives_projection() -> None:
    # A term that cannot pass the projection's own label check would be retained
    # in the vocabulary but dropped at write time -- silently unretained evidence.
    for key, terms in METADATA_VOCABULARIES.items():
        for term in terms:
            assert term == term.lower(), f"{key}: {term!r} is not lower-case"
            assert term.strip() == term and term, f"{key}: {term!r} is not a bare label"
