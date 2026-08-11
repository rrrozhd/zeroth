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

This fixture therefore pins the ordered
``(path, methods, name, permission, public)`` contract for every route. The
permission and public fields come from the same authoritative registry used by
the default-deny middleware, so adding a route requires an explicit policy and
an inventory update.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from zeroth.service.api.route_authorization import (
    PUBLIC_ROUTE_NAMES,
    permission_for_route_name,
)
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
        name = getattr(route, "name", None)
        permission = permission_for_route_name(name)
        inventory.append(
            {
                "kind": type(route).__name__,
                "path": getattr(route, "path", None),
                "methods": sorted(getattr(route, "methods", None) or []),
                "name": name,
                "permission": permission.value if permission is not None else None,
                "public": name in PUBLIC_ROUTE_NAMES,
            }
        )
    return inventory


def test_route_inventory_matches_ordered_snapshot() -> None:
    """Route order, identity, and authorization policy are all contract."""
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
