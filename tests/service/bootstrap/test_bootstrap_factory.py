"""Canonical import surface for the extracted bootstrap factory."""

from __future__ import annotations


def test_factory_is_the_same_object_through_both_paths() -> None:
    from zeroth.core.service.bootstrap import bootstrap_app as legacy_app
    from zeroth.core.service.bootstrap import bootstrap_service as legacy_service
    from zeroth.service.bootstrap.factory import bootstrap_app, bootstrap_service

    assert bootstrap_service is legacy_service
    assert bootstrap_app is legacy_app
