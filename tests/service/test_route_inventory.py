"""Characterization of the exact HTTP route inventory.

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

This fixture therefore pins the ordered ``(path, methods, name)`` triple for
every route, which is exactly what the decomposition of the service package
must preserve.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from zeroth.service.app import create_app

FIXTURE = (
    Path(__file__).resolve().parents[1] / "contracts" / "fixtures" / "backend_route_inventory.json"
)


def current_route_inventory() -> list[dict[str, Any]]:
    """Return the ordered route inventory of a deployment-less application."""
    app = create_app(SimpleNamespace(regulus_client=None))
    inventory: list[dict[str, Any]] = []
    for route in app.routes:
        inventory.append(
            {
                "kind": type(route).__name__,
                "path": getattr(route, "path", None),
                "methods": sorted(getattr(route, "methods", None) or []),
                "name": getattr(route, "name", None),
            }
        )
    return inventory


def test_route_inventory_matches_ordered_snapshot() -> None:
    """Route order, paths, methods, and endpoint names are all contract."""
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
