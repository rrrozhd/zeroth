"""Service wrapper package for deployment-bound HTTP APIs.

The exports resolve lazily. This package init is the re-entry point for a
legacy-to-canonical import cycle: every ``zeroth.core.service.X`` import
executes this init first, and the legacy ``.bootstrap`` module is a shim over
``zeroth.service.bootstrap``. If this init imported ``.bootstrap`` eagerly, a
``zeroth.core.service.X`` import made from inside a still-initializing
``zeroth.service.bootstrap`` module would re-enter that partially initialized
module and fail — but only in a cold interpreter, which the in-process suite
never is. Laziness here is load-bearing, exactly like the ``zeroth.core.econ``,
``http``, and ``runs`` package inits; the guard is
``tests/service/test_cold_import.py``.
"""

from __future__ import annotations

import importlib

_EXPORTS = {
    "DeploymentBootstrapError": "zeroth.core.service.bootstrap",
    "ServiceBootstrap": "zeroth.core.service.bootstrap",
    "bootstrap_app": "zeroth.core.service.bootstrap",
    "bootstrap_service": "zeroth.core.service.bootstrap",
    "create_app": "zeroth.core.service.app",
}

__all__ = [
    "DeploymentBootstrapError",
    "ServiceBootstrap",
    "bootstrap_app",
    "bootstrap_service",
    "create_app",
]


def __getattr__(name: str) -> object:
    """Resolve the service exports from their defining modules on first access."""
    module = _EXPORTS.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_EXPORTS))
