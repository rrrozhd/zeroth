"""Exact Agent Server endpoint inventory supported by the foundation gateway."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from zeroth.contracts.langgraph_gateway.models import EndpointKind, RouteDisposition


@dataclass(frozen=True, slots=True)
class EndpointRule:
    """An immutable, anchored method/path classification rule."""

    method: str
    path_template: str
    path_pattern: re.Pattern[str] = field(repr=False, compare=False)
    disposition: RouteDisposition | None
    operation: str
    kind: EndpointKind = EndpointKind.HTTP

    def matches(self, method: str, path: str) -> bool:
        """Return whether both the normalized method and entire path match."""
        return self.method == method.upper() and self.path_pattern.fullmatch(path) is not None


def _rule(
    method: str,
    path_template: str,
    pattern: str,
    disposition: RouteDisposition | None,
    operation: str,
    *,
    kind: EndpointKind = EndpointKind.HTTP,
) -> EndpointRule:
    return EndpointRule(
        method=method,
        path_template=path_template,
        path_pattern=re.compile(pattern),
        disposition=disposition,
        operation=operation,
        kind=kind,
    )


_ID = r"[^/]+"

ENDPOINT_RULES: tuple[EndpointRule, ...] = (
    _rule("GET", "/ok", r"^/ok$", RouteDisposition.TRANSPARENT, "system.ok"),
    _rule("GET", "/info", r"^/info$", RouteDisposition.TRANSPARENT, "system.info"),
    _rule(
        "GET",
        "/openapi.json",
        r"^/openapi\.json$",
        RouteDisposition.TRANSPARENT,
        "system.openapi",
    ),
    _rule(
        "POST",
        "/assistants",
        r"^/assistants$",
        RouteDisposition.TRANSPARENT,
        "assistants.create",
    ),
    _rule(
        "GET",
        "/assistants/{assistant_id}",
        rf"^/assistants/{_ID}$",
        RouteDisposition.TRANSPARENT,
        "assistants.get",
    ),
    _rule(
        "GET",
        "/assistants/{assistant_id}/graph",
        rf"^/assistants/{_ID}/graph$",
        RouteDisposition.TRANSPARENT,
        "assistants.graph",
    ),
    _rule(
        "POST",
        "/assistants/search",
        r"^/assistants/search$",
        RouteDisposition.TRANSPARENT,
        "assistants.search",
    ),
    _rule("POST", "/threads", r"^/threads$", RouteDisposition.TRANSPARENT, "threads.create"),
    _rule(
        "GET",
        "/threads/{thread_id}",
        rf"^/threads/{_ID}$",
        RouteDisposition.TRANSPARENT,
        "threads.get",
    ),
    _rule(
        "POST",
        "/threads/search",
        r"^/threads/search$",
        RouteDisposition.TRANSPARENT,
        "threads.search",
    ),
    _rule(
        "GET",
        "/threads/{thread_id}/stream",
        rf"^/threads/{_ID}/stream$",
        RouteDisposition.TRANSPARENT,
        "threads.stream",
    ),
    _rule(
        "GET",
        "/threads/{thread_id}/state",
        rf"^/threads/{_ID}/state$",
        RouteDisposition.TRANSPARENT,
        "state.get",
    ),
    _rule(
        "POST",
        "/threads/{thread_id}/state",
        rf"^/threads/{_ID}/state$",
        RouteDisposition.TRANSPARENT,
        "state.update",
    ),
    _rule(
        "POST",
        "/threads/{thread_id}/state/checkpoint",
        rf"^/threads/{_ID}/state/checkpoint$",
        RouteDisposition.TRANSPARENT,
        "state.checkpoint",
    ),
    _rule(
        "POST",
        "/threads/{thread_id}/history",
        rf"^/threads/{_ID}/history$",
        RouteDisposition.TRANSPARENT,
        "history.list",
    ),
    _rule(
        "POST",
        "/threads/{thread_id}/runs",
        rf"^/threads/{_ID}/runs$",
        RouteDisposition.GOVERNED,
        "runs.create",
    ),
    _rule(
        "POST",
        "/threads/{thread_id}/runs/stream",
        rf"^/threads/{_ID}/runs/stream$",
        RouteDisposition.GOVERNED,
        "runs.stream",
    ),
    _rule(
        "POST",
        "/threads/{thread_id}/runs/wait",
        rf"^/threads/{_ID}/runs/wait$",
        RouteDisposition.GOVERNED,
        "runs.wait",
    ),
    _rule(
        "POST",
        "/runs/stream",
        r"^/runs/stream$",
        RouteDisposition.GOVERNED,
        "runs.stateless_stream",
    ),
    _rule(
        "POST",
        "/runs/wait",
        r"^/runs/wait$",
        RouteDisposition.GOVERNED,
        "runs.stateless_wait",
    ),
    _rule(
        "GET",
        "/threads/{thread_id}/runs/{run_id}",
        rf"^/threads/{_ID}/runs/{_ID}$",
        RouteDisposition.TRANSPARENT,
        "runs.get",
    ),
    _rule(
        "GET",
        "/threads/{thread_id}/runs/{run_id}/join",
        rf"^/threads/{_ID}/runs/{_ID}/join$",
        RouteDisposition.TRANSPARENT,
        "runs.join",
    ),
    _rule(
        "GET",
        "/threads/{thread_id}/runs/{run_id}/stream",
        rf"^/threads/{_ID}/runs/{_ID}/stream$",
        RouteDisposition.TRANSPARENT,
        "runs.lifecycle_stream",
    ),
    _rule(
        "POST",
        "/threads/{thread_id}/runs/{run_id}/cancel",
        rf"^/threads/{_ID}/runs/{_ID}/cancel$",
        RouteDisposition.TRANSPARENT,
        "runs.cancel",
    ),
    _rule(
        "POST",
        "/threads/{thread_id}/commands",
        rf"^/threads/{_ID}/commands$",
        None,
        "protocol.command",
        kind=EndpointKind.PROTOCOL_COMMAND,
    ),
    _rule(
        "POST",
        "/threads/{thread_id}/stream/events",
        rf"^/threads/{_ID}/stream/events$",
        RouteDisposition.TRANSPARENT,
        "protocol.events",
        kind=EndpointKind.PROTOCOL_EVENT_STREAM,
    ),
    _rule(
        "WS",
        "/threads/{thread_id}/stream/events",
        rf"^/threads/{_ID}/stream/events$",
        RouteDisposition.TRANSPARENT,
        "protocol.events",
        kind=EndpointKind.PROTOCOL_EVENT_STREAM,
    ),
)

_GOVERNED_PROTOCOL_METHODS = frozenset({"run.start", "input.respond"})
_TRANSPARENT_PROTOCOL_METHODS = frozenset(
    {"agent.getTree", "subscription.subscribe", "subscription.unsubscribe"}
)


def classify_endpoint(method: str, path: str) -> EndpointRule | None:
    """Return the exact claimed rule, or ``None`` for unsupported endpoints."""
    return next((rule for rule in ENDPOINT_RULES if rule.matches(method, path)), None)


def classify_protocol_command(payload: Any) -> RouteDisposition:
    """Classify a protocol-v2 command envelope by its exact known method."""
    if not isinstance(payload, dict):
        return RouteDisposition.UNSUPPORTED
    method = payload.get("method")
    if method in _GOVERNED_PROTOCOL_METHODS:
        return RouteDisposition.GOVERNED
    if method in _TRANSPARENT_PROTOCOL_METHODS:
        return RouteDisposition.TRANSPARENT
    return RouteDisposition.UNSUPPORTED
