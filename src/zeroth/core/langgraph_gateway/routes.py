"""Legacy import path for the gateway routes module.

It now lives in :mod:`zeroth.service.langgraph_gateway.routes`; this module
republishes exactly the names it published before ZER-24 relocated it. Import
from the canonical location instead (see docs/backend-import-migration.md).

Resolution is lazy on purpose: importing this shim must not drag the
``service`` package onto the import path of anything that merely touches
``zeroth.core.langgraph_gateway``.

The export list is the surface captured *before* the move, in
``tests/langgraph_gateway/fixtures/legacy_surface_manifest.json``. It is wider than a curated
``__all__`` because this module never declared one -- its public surface was
whatever a star-import saw, incidental imports included -- and narrowing it now
would break a caller that relied on the old behaviour.

The shim declares that surface as ``__all__`` even though the pre-move module did
not. Overriding ``__dir__`` alone is not enough: ``from X import *`` reads the
module namespace and never consults ``__dir__``, so a lazy shim without
``__all__`` exports only its own globals. Declaring it is what keeps the
observable star-import surface identical.
"""

from __future__ import annotations

from typing import Any

_EXPORTS = frozenset(
    {
        "APIRouter",
        "AdmissionRequest",
        "Any",
        "AuthenticatedPrincipal",
        "AuthenticationError",
        "BudgetChecker",
        "Callable",
        "CompatibilityResult",
        "CompatibilityStatus",
        "FastAPI",
        "GatewayContextError",
        "GatewayWebSocketEndpoint",
        "HTTPGatewayProxy",
        "HTTPGatewayTransport",
        "InputClassifier",
        "LangGraphGatewaySettings",
        "PolicyAdmissionChecker",
        "Protocol",
        "Request",
        "ReservedContextClaims",
        "ReservedContextCodec",
        "RouteDisposition",
        "ServiceAuthenticator",
        "UnclassifiedInputClassifier",
        "UpstreamCredentialUnavailableError",
        "ValidationError",
        "WebSocket",
        "WebSocketClientError",
        "WebSocketGatewayCloseError",
        "WebSocketGatewayHandler",
        "WebSocketMessage",
        "WebSocketRoute",
        "admit",
        "annotations",
        "classify_protocol_command",
        "dataclass",
        "inject_reserved_context",
        "json",
        "register_gateway_routes",
        "set_correlation_id",
        "time",
        "uuid4",
    }
)

# Declared even though the pre-move module did not declare one. ``from X import *``
# reads the module namespace when ``__all__`` is absent -- it never consults
# ``__dir__`` -- so without this a star-import of this shim would yield only the
# shim's own globals and silently drop the recorded surface. Laziness is unaffected:
# each name still resolves through ``__getattr__`` when it is actually read.
__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    # Every non-dunder name delegates, not only the recorded ones. ``_EXPORTS``
    # is the *declared* surface and is what ``__dir__`` reports, but a module
    # that declared ``__all__`` still let callers reach attributes outside it --
    # ``proxy.TeeObserver`` is one such caller in the test suite. Narrowing to
    # ``_EXPORTS`` here would silently break them.
    if name.startswith("__") and name.endswith("__"):
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import zeroth.service.langgraph_gateway.routes as _canonical

    try:
        return getattr(_canonical, name)
    except AttributeError:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg) from None


def __dir__() -> list[str]:
    return sorted(_EXPORTS)
