"""Backend service composition.

The exports resolve lazily. Eager imports here would load the FastAPI app
closure on any ``zeroth.service.*`` submodule import, so a caller that only
wanted a bootstrap helper would pay for the whole service shell.
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

# The ``# noqa: F822`` markers below are for the commit gate, which lints a copy
# of the staged file outside the package tree. Without that context Ruff cannot
# see that ``__getattr__`` resolves these names, so it reports them as undefined.
# In-tree ``ruff check`` passes without them.
__all__ = [
    "DeploymentBootstrapError",  # noqa: F822
    "ServiceBootstrap",  # noqa: F822
    "bootstrap_app",  # noqa: F822
    "bootstrap_service",  # noqa: F822
    "create_app",  # noqa: F822
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
