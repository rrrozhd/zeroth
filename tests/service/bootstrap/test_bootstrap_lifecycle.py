"""Canonical import surface for the extracted service lifespan."""

from __future__ import annotations


def test_app_module_uses_the_extracted_lifespan() -> None:
    """The app factory binds the lifespan from the bootstrap package.

    Starlette wraps the lifespan in a merged context object at app
    construction, so identity is asserted on the module binding the factory
    passes to FastAPI rather than on ``app.router.lifespan_context``. The
    lifespan's cleanup behavior itself is characterized by
    ``tests/service/test_app.py::test_lifespan_closes_secret_provider_exactly_once``.
    """
    from zeroth.service import app as app_module
    from zeroth.service.bootstrap.lifecycle import service_lifespan

    assert app_module.service_lifespan is service_lifespan
