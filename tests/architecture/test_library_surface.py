"""Characterization tests for Zeroth's supported backend library surface.

The legacy snapshot is a capability contract, not a promise that old import
locations remain forever.  The canonical snapshot maps each protected legacy
capability to its current import location and is updated alongside the backend
import migration guide when ownership changes.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
import warnings
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel


FIXTURES = Path(__file__).parents[1] / "contracts" / "fixtures"
REPO_ROOT = Path(__file__).parents[2]
CANONICAL_PACKAGES = (
    "zeroth.runtime",
    "zeroth.runtime.orchestration",
    "zeroth.runtime.agents",
    "zeroth.runtime.context",
    "zeroth.runtime.parallel",
    "zeroth.runtime.runs",
    "zeroth.runtime.subgraphs",
    "zeroth.governance",
    "zeroth.governance.approvals",
    "zeroth.governance.audit",
    "zeroth.governance.identity",
    "zeroth.governance.policy",
    "zeroth.governance.guardrails",
    "zeroth.governance.retention",
    "zeroth.platform",
    "zeroth.platform.artifacts",
    "zeroth.platform.config",
    "zeroth.platform.dispatch",
    "zeroth.platform.observability",
    "zeroth.platform.persistence",
    "zeroth.platform.primitives",
    "zeroth.platform.secrets",
    "zeroth.platform.signing",
    "zeroth.platform.storage",
    "zeroth.contracts",
    "zeroth.contracts.conditions",
    "zeroth.contracts.governed",
    "zeroth.contracts.graph",
    "zeroth.contracts.registry",
    "zeroth.contracts.mappings",
    "zeroth.contracts.templates",
    "zeroth.service",
    "zeroth.service.api",
    "zeroth.service.bootstrap",
    "zeroth.service.deployments",
    "zeroth.service.webhooks",
    "zeroth.econ",
    "zeroth.econ.analytics",
    "zeroth.econ.instrumentation",
    "zeroth.econ.plane",
    "zeroth.integrations",
    "zeroth.integrations.execution",
    "zeroth.integrations.http",
    "zeroth.integrations.memory",
    "zeroth.integrations.persistence",
    "zeroth.integrations.rag",
    "zeroth.integrations.sandbox",
    "zeroth.eval",
)


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.parametrize("module_name", CANONICAL_PACKAGES)
def test_every_canonical_package_imports(module_name: str) -> None:
    """The authoritative backend package tree must remain importable."""
    importlib.import_module(module_name)


def _import_symbol(entry: dict[str, Any]) -> object:
    module = importlib.import_module(entry["module"])
    try:
        return getattr(module, entry["name"])
    except AttributeError:
        # Package __all__ may publish a lazily imported submodule.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"\s*on_event is deprecated.*",
                category=DeprecationWarning,
            )
            return importlib.import_module(f"{entry['module']}.{entry['name']}")


def _signature(value: object) -> str | None:
    if not callable(value):
        return None
    try:
        signature = str(inspect.signature(value))
        return re.sub(r" at 0x[0-9a-fA-F]+", " at 0x<address>", signature)
    except (TypeError, ValueError):
        # Some Python exception classes inherit an opaque built-in constructor.
        # Calling inspect.signature is still part of the smoke test; the marker
        # makes that interpreter-level fact explicit in the protected contract.
        return "<not-inspectable>"


def test_immutable_legacy_capabilities_remain_available_with_original_signatures() -> None:
    """Legacy capabilities may move, but they cannot silently disappear or change."""
    legacy = _load("backend_surface_legacy.json")
    canonical = _load("backend_surface_canonical.json")

    assert legacy["immutable"] is True
    assert canonical["evolving"] is True

    current_by_legacy_id: dict[str, dict[str, Any]] = {}
    for entry in canonical["symbols"]:
        for legacy_id in entry["legacy_ids"]:
            assert legacy_id not in current_by_legacy_id, f"duplicate legacy mapping: {legacy_id}"
            current_by_legacy_id[legacy_id] = entry

    missing = [
        capability["id"]
        for capability in legacy["capabilities"]
        if capability["id"] not in current_by_legacy_id
    ]
    assert not missing, f"legacy capabilities missing canonical replacements: {missing}"

    mismatches = []
    for capability in legacy["capabilities"]:
        current = current_by_legacy_id[capability["id"]]
        if current["signature"] != capability["signature"]:
            mismatches.append(
                {
                    "capability": capability["id"],
                    "expected": capability["signature"],
                    "canonical": current["signature"],
                }
            )
    assert not mismatches, f"legacy signature changes: {mismatches}"


@pytest.mark.parametrize(
    "entry",
    _load("backend_surface_canonical.json")["symbols"],
    ids=lambda entry: f"{entry['module']}:{entry['name']}",
)
def test_every_canonical_symbol_imports_and_matches_its_signature(entry: dict[str, Any]) -> None:
    """The evolving canonical fixture is executable import documentation."""
    value = _import_symbol(entry)
    assert _signature(value) == entry["signature"]


def test_surface_inventory_records_all_required_evidence_classes() -> None:
    """Guard against rebuilding the inventory from only package exports."""
    legacy = _load("backend_surface_legacy.json")
    evidence_classes = {
        evidence.split(":", 1)[0]
        for capability in legacy["capabilities"]
        for evidence in capability["evidence"]
    }
    assert {
        "__all__",
        "docs",
        "entry_point",
        "examples",
        "optional_integration",
        "package_export",
        "schema_model",
    } <= evidence_classes


def _discover_schema_models() -> set[str]:
    """Discover models in the same schema-bearing modules covered by the inventory."""
    source_root = REPO_ROOT / "src"
    zeroth_root = source_root / "zeroth"
    schema_paths = {
        path
        for path in zeroth_root.rglob("*.py")
        if path.name in {"models.py", "schemas.py"}
        or (
            path.parent.name == "service"
            and (
                path.name.endswith("_api.py")
                or path.name in {"app.py", "health.py", "studio_schemas.py"}
            )
        )
    }
    discovered: set[str] = set()
    for path in schema_paths:
        module_name = ".".join(path.relative_to(source_root).with_suffix("").parts)
        module = importlib.import_module(module_name)
        for name, value in vars(module).items():
            if (
                not name.startswith("_")
                and inspect.isclass(value)
                and value.__module__ == module_name
                and issubclass(value, BaseModel)
            ):
                discovered.add(f"{module_name}:{name}")
    return discovered


def test_every_discovered_schema_model_is_in_legacy_and_canonical_surfaces() -> None:
    """Reverse coverage prevents schema modules from being silently omitted."""
    legacy = _load("backend_surface_legacy.json")
    canonical = _load("backend_surface_canonical.json")
    legacy_ids = {entry["id"] for entry in legacy["capabilities"]}
    canonical_by_current_id = {
        f"{entry['module']}:{entry['name']}": entry for entry in canonical["symbols"]
    }
    discovered = _discover_schema_models()

    missing_canonical = sorted(discovered - canonical_by_current_id.keys())
    assert not missing_canonical, f"schema models missing canonical entries: {missing_canonical}"

    missing_legacy = sorted(
        current_id
        for current_id in discovered
        if not (set(canonical_by_current_id[current_id]["legacy_ids"]) & legacy_ids)
    )
    assert not missing_legacy, f"schema models missing protected legacy IDs: {missing_legacy}"
