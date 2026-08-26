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
from zeroth.governance.audit.capture_projection import ContentFreeProjection
from zeroth.governance.audit.capture_vocabulary import (
    METADATA_VOCABULARIES,
    REASON_CODES,
)
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


# ZER-37: repository-unit provenance keys, with values shaped exactly the way
# the runtime mints them. The tripwire this pins (AC-6): a key registered in
# METADATA_KINDS whose real value does not fit its declared kind is silently
# summarized at write time, so the audit row would claim the key while holding
# a digest blob instead of the value. Projecting representative values proves
# post-projection presence, not just registration.
_REPOSITORY_PROVENANCE_SAMPLES: dict[str, object] = {
    "checkout_id": "co-1f2e3d4c",
    "repo_installation_id": "8891",
    "repo_repository_id": "4242",
    # Bare 40-hex, exactly as StagedCheckout.commit_sha carries it; the DIGEST
    # shape check admits an unprefixed hex digest.
    "repo_commit_sha": "0123456789abcdef0123456789abcdef01234567",
    "repo_config_digest": "sha256:" + "c" * 64,
    "repo_tree_digest": "sha256:" + "d" * 64,
    # compute_manifest_digest returns bare 64-hex.
    "repo_manifest_digest": "e" * 64,
    "smoke_passed": True,
}

_REPOSITORY_REASON_CODES = frozenset(
    {
        "checkout_unavailable_error",
        "installation_revoked",
        "repo_manifest_validation_error",
        "script_not_declared",
        "smoke_assertion_failed",
    }
)


def test_every_repository_provenance_key_survives_the_capture_projection() -> None:
    projection = ContentFreeProjection(scrub=lambda value: value)

    kept, summary = projection.metadata(_REPOSITORY_PROVENANCE_SAMPLES)

    assert kept == _REPOSITORY_PROVENANCE_SAMPLES
    assert summary["dropped_keys"] == 0
    for key, value in kept.items():
        assert kept[key] == value and not isinstance(kept[key], dict), (
            f"{key} was summarized instead of retained"
        )


def test_the_repository_reason_codes_are_registered_and_survive_projection() -> None:
    assert _REPOSITORY_REASON_CODES <= REASON_CODES
    projection = ContentFreeProjection(scrub=lambda value: value)
    for code in sorted(_REPOSITORY_REASON_CODES):
        kept, _ = projection.metadata({"reason_code": code})
        assert kept == {"reason_code": code}, f"{code} did not survive projection"


def test_every_vocabulary_term_is_label_shaped_so_it_survives_projection() -> None:
    # A term that cannot pass the projection's own label check would be retained
    # in the vocabulary but dropped at write time -- silently unretained evidence.
    for key, terms in METADATA_VOCABULARIES.items():
        for term in terms:
            assert term == term.lower(), f"{key}: {term!r} is not lower-case"
            assert term.strip() == term and term, f"{key}: {term!r} is not a bare label"
