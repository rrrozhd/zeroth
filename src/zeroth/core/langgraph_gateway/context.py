"""Legacy import path for the gateway context module.

It now lives in :mod:`zeroth.service.langgraph_gateway.context`; this module
republishes exactly the names it published before ZER-24 relocated it. Import
from the canonical location instead (see docs/backend-import-migration.md).

Resolution is lazy on purpose: importing this shim must not drag the
``service`` package onto the import path of anything that merely touches
``zeroth.core.langgraph_gateway``.

The export list is the surface captured *before* the move, in
``tests/langgraph_gateway/fixtures/legacy_surface_manifest.json``. The module declared ``__all__``
before the move, so the shim keeps declaring the same one; ``__dir__`` is
overridden so the surface survives lazy resolution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zeroth.service.langgraph_gateway.context import (
        GatewayContextError,
        ReservedContextClaims,
        ReservedContextCodec,
        inject_reserved_context,
    )

_EXPORTS = frozenset(
    {
        "GatewayContextError",
        "ReservedContextClaims",
        "ReservedContextCodec",
        "inject_reserved_context",
    }
)

__all__ = [
    "GatewayContextError",
    "ReservedContextClaims",
    "ReservedContextCodec",
    "inject_reserved_context",
]


def __getattr__(name: str) -> Any:
    # Every non-dunder name delegates, not only the recorded ones. ``_EXPORTS``
    # is the *declared* surface and is what ``__dir__`` reports, but a module
    # that declared ``__all__`` still let callers reach attributes outside it --
    # ``proxy.TeeObserver`` is one such caller in the test suite. Narrowing to
    # ``_EXPORTS`` here would silently break them.
    if name.startswith("__") and name.endswith("__"):
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import zeroth.service.langgraph_gateway.context as _canonical

    try:
        return getattr(_canonical, name)
    except AttributeError:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg) from None


def __dir__() -> list[str]:
    return sorted(_EXPORTS)
