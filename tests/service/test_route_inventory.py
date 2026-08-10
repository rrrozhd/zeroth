"""Characterization of the exact backend route and authorization inventory.

The OpenAPI snapshot in ``tests/contracts/test_refactor_contract_snapshots.py``
normalizes paths into a sorted mapping and strips ``operationId`` as prose
noise. Neither route *order* nor endpoint *identity* survives that
normalization, yet both are load-bearing:

* FastAPI resolves a request against routes in registration order, so moving a
  literal path such as ``/v1/runs/active`` behind a parameterized
  ``/v1/runs/{run_id}`` silently changes which handler serves it.
* ``operationId`` is derived from the endpoint function name and is a published
  identifier for generated clients, so renaming or re-binding a handler is a
  breaking change.

This fixture therefore pins the ordered ``(path, methods, name, permission)``
contract for every route.  ``permission`` initially characterizes the checks
performed by today's endpoint bodies; a later default-deny router policy can
replace that discovery mechanism without losing the before-state.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
from pathlib import Path
import textwrap
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from zeroth.service.app import create_app

FIXTURE = (
    Path(__file__).resolve().parents[1] / "contracts" / "fixtures" / "backend_route_inventory.json"
)


def current_route_inventory() -> list[dict[str, Any]]:
    """Return the ordered route inventory, including conditional gateway routes."""
    # The optional console mount depends on deploy-time assets and is covered by
    # tests/test_console_ui.py. Pin it absent here so a local `npm run build`
    # cannot change the backend API characterization snapshot.
    with patch.dict(os.environ, {"ZEROTH_CONSOLE_DIR": "/__zeroth_route_inventory_no_console__"}):
        app = create_app(
            SimpleNamespace(
                authenticator=object(),
                langgraph_gateway_compatibility=None,
                langgraph_gateway_proxy=object(),
                langgraph_gateway_websocket_handler=object(),
                regulus_client=None,
            )
        )
    inventory: list[dict[str, Any]] = []
    for route in app.routes:
        inventory.append(
            {
                "kind": type(route).__name__,
                "path": getattr(route, "path", None),
                "methods": sorted(getattr(route, "methods", None) or []),
                "name": getattr(route, "name", None),
                "permission": _permission_used_by(getattr(route, "endpoint", None)),
            }
        )
    return inventory


def _permission_used_by(endpoint: object) -> str | None:
    """Return the one explicit ``Permission`` referenced by an endpoint body."""
    if not callable(endpoint):
        return None
    try:
        source = textwrap.dedent(inspect.getsource(endpoint))
    except (OSError, TypeError):
        return None
    references = {
        node.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Permission"
    }
    if len(references) > 1:
        raise AssertionError(
            f"route endpoint {getattr(endpoint, '__name__', endpoint)!r} "
            f"references multiple permissions: {sorted(references)}"
        )
    if not references:
        return None
    from zeroth.service.api.authorization import Permission

    return Permission[next(iter(references))].value


def test_route_inventory_matches_ordered_snapshot() -> None:
    """Route order, identity, and today's permission checks are all contract."""
    expected = json.loads(FIXTURE.read_text())
    assert current_route_inventory() == expected


def test_route_inventory_has_no_duplicate_path_method_pairs() -> None:
    """A duplicated (path, method) pair means a later route is unreachable."""
    seen: set[tuple[str | None, str]] = set()
    duplicates: list[tuple[str | None, str]] = []
    for entry in current_route_inventory():
        for method in entry["methods"]:
            pair = (entry["path"], method)
            if pair in seen:
                duplicates.append(pair)
            seen.add(pair)
    assert not duplicates, f"unreachable duplicate routes: {duplicates}"
