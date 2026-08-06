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

__all__ = [
    "DeploymentBootstrapError",  # noqa: F822 - resolved lazily by __getattr__
    "ServiceBootstrap",  # noqa: F822 - resolved lazily by __getattr__
    "bootstrap_app",  # noqa: F822 - resolved lazily by __getattr__
    "bootstrap_service",  # noqa: F822 - resolved lazily by __getattr__
    "create_app",  # noqa: F822 - resolved lazily by __getattr__
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
