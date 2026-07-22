"""Backend service composition.

The exports resolve lazily, mirroring the legacy ``zeroth.core.service``
shell: eager imports here would load the FastAPI app closure on any
``zeroth.service.*`` submodule import, and the legacy shims import those
submodules while this package initializes.
"""

from __future__ import annotations

import importlib

_EXPORTS = {
    "DeploymentBootstrapError": "zeroth.service.bootstrap.container",
    "ServiceBootstrap": "zeroth.service.bootstrap.container",
    "bootstrap_app": "zeroth.service.bootstrap.factory",
    "bootstrap_service": "zeroth.service.bootstrap.factory",
    "create_app": "zeroth.service.app",
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
