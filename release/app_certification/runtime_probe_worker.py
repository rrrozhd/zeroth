"""Standalone exact-runtime optional-extra probe read over container stdin."""

from __future__ import annotations

import importlib
import importlib.metadata
import sys
from pathlib import Path


def probe_runtime_extras(zeroth_version: str) -> None:
    """Require the exact installed distribution and its Regulus capabilities."""
    distribution = importlib.metadata.distribution("zeroth-core")
    if distribution.version != zeroth_version:
        raise ValueError(
            f"runtime zeroth-core version mismatch: {distribution.version} != {zeroth_version}"
        )
    site_packages = Path(distribution.locate_file("")).resolve()
    modules = [
        importlib.import_module(name)
        for name in (
            "zeroth.econ.analytics.budget",
            "zeroth.econ.instrumentation",
            "zeroth.econ.plane.main",
        )
    ]
    for module in modules:
        origin = Path(module.__file__).resolve()
        if not origin.is_relative_to(site_packages):
            raise ValueError(f"runtime optional extra escaped the installed distribution: {origin}")
    routes = {route.path for route in modules[-1].app.routes}
    required = {
        "/v1/auth/token",
        "/v1/budget/tenants/{tenant_id}",
        "/v1/capabilities",
        "/v1/instrumentation/executions",
    }
    if missing := sorted(required - routes):
        raise ValueError(f"runtime Regulus capabilities are missing: {', '.join(missing)}")


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) != 1:
        raise ValueError("exact-runtime probe requires one Zeroth version")
    probe_runtime_extras(values[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
