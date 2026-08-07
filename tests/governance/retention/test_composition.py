"""The canonical retention package composes the decomposed erasure surface.

Two identity rules hold everything published here together. The canonical
package republishes the *same objects* the legacy paths export -- redefining
them would change ``__module__`` and break the pinned legacy signatures -- and
the relocated econ adapter keeps answering from its legacy path for the same
reason in the other direction.
"""

from __future__ import annotations


def test_canonical_package_exports_the_composed_surface() -> None:
    import zeroth.governance.retention as retention

    for name in (
        "CleanupClaims",
        "CleanupExecutor",
        "CleanupReplayState",
        "CompatibilityLog",
        "LegalHoldError",
        "RetentionErasureService",
        "StaleCleanupClaimError",
        "build_cleanup_manifest",
        "manifest_complete",
        "replay_cleanup_state",
        "result_from_manifest",
    ):
        assert name in retention.__all__
        assert hasattr(retention, name)


def test_canonical_service_is_the_protected_class_object() -> None:
    """One class object, however it is imported -- not a parallel definition."""
    from zeroth.governance.retention import RetentionErasureService as Canonical
    from zeroth.governance.retention import RetentionErasureService as LegacyModule
    from zeroth.governance.retention import RetentionErasureService as LegacyPackage
    from zeroth.governance.retention.service import RetentionErasureService as CanonicalModule

    assert Canonical is LegacyPackage is LegacyModule is CanonicalModule


def test_canonical_errors_are_the_protected_class_objects() -> None:
    from zeroth.governance.retention import (
        LegalHoldError as CanonicalHold,
    )
    from zeroth.governance.retention import LegalHoldError as LegacyHold
    from zeroth.governance.retention import (
        StaleCleanupClaimError as CanonicalStale,
    )
    from zeroth.governance.retention import StaleCleanupClaimError as LegacyStale

    assert CanonicalHold is LegacyHold
    assert CanonicalStale is LegacyStale


def test_the_econ_adapter_moved_to_the_econ_domain_and_still_answers_legacy_imports() -> None:
    """The concrete SQLAlchemy eraser is econ-plane code, not retention code.

    Its presence in ``zeroth.core.retention`` was the only reason the governance
    domain imported ``zeroth.econ_plane`` -- the two dependency exceptions this
    task removes. The protocol stays with retention; the adapter lives with the
    database it deletes from.
    """
    from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser as Canonical
    from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser as LegacyModule
    from zeroth.econ.plane.erasure import SqlAlchemyEconEventEraser as LegacyPackage
    from zeroth.governance.retention import EconEventEraser

    assert Canonical is LegacyPackage is LegacyModule
    assert Canonical.__module__ == "zeroth.econ.plane.erasure"
    assert isinstance(Canonical(), EconEventEraser)
