"""Service bootstrap composition.

Configuration defaults, Alembic migrations, the dependency container, the
factory, and the application lifespan each own one concern. The exports
resolve lazily for the same reason as ``zeroth.service``: importing one
concern must not execute the other four.
"""

from __future__ import annotations

import importlib

_EXPORTS = {
    "DeploymentBootstrapError": "zeroth.service.bootstrap.container",
    "ServiceBootstrap": "zeroth.service.bootstrap.container",
    "bootstrap_app": "zeroth.service.bootstrap.factory",
    "bootstrap_service": "zeroth.service.bootstrap.factory",
    "run_migrations": "zeroth.service.bootstrap.migrations",
    "service_lifespan": "zeroth.service.bootstrap.lifecycle",
}

# The ``# noqa: F822`` markers below are for the commit gate, which lints a copy
# of the staged file outside the package tree. Without that context Ruff cannot
# see that ``__getattr__`` resolves these names, so it reports them as undefined.
# In-tree ``ruff check`` passes without them.
__all__ = [
    "DeploymentBootstrapError",  # noqa: F822
    "ServiceBootstrap",  # noqa: F822
    "bootstrap_app",  # noqa: F822
    "bootstrap_service",  # noqa: F822
    "run_migrations",  # noqa: F822
    "service_lifespan",  # noqa: F822
]


def __getattr__(name: str) -> object:
    """Resolve the bootstrap exports from their defining modules on first access."""
    module = _EXPORTS.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(_EXPORTS))
