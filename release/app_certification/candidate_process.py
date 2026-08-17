"""Serialize candidate objects without loading trusted finalizer state."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import io
import json
import sys
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from zeroth.contracts.graph import Graph
from zeroth.platform.config import get_settings
from zeroth.runtime.parallel.reducers import resolve_reducer_ref
from zeroth.service.api.authentication import ServiceAuthConfig

from .models import AppDeclaration, file_digest


def _load_target(reference: str) -> Any:
    module_name, _, attribute_path = reference.partition(":")
    value: Any = importlib.import_module(module_name)
    for attribute in attribute_path.split("."):
        value = getattr(value, attribute)
    return value


def _contracts(declaration: AppDeclaration) -> dict[str, type[BaseModel]]:
    value = _load_target(declaration.targets.contracts)
    if not isinstance(value, Mapping) or not value:
        raise ValueError("contracts target must be a non-empty mapping")
    contracts = dict(value)
    for name, model in contracts.items():
        if not isinstance(name, str) or not name or not isinstance(model, type):
            raise ValueError("contracts must map non-empty names to Pydantic model types")
        if not issubclass(model, BaseModel):
            raise ValueError(f"contract {name!r} is not a Pydantic model type")
    return contracts


def _graphs(declaration: AppDeclaration) -> list[Any]:
    graphs = [_load_target(reference)() for reference in declaration.targets.graph_builders]
    if any(not isinstance(graph, Graph) for graph in graphs):
        raise ValueError("every graph builder must return a Graph")
    return graphs


def _schemas(contracts: Mapping[str, type[BaseModel]]) -> dict[str, dict[str, Any]]:
    return {name: model.model_json_schema() for name, model in contracts.items()}


def _graph_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    graphs = _graphs(declaration)
    contracts = _contracts(declaration)
    reducers = {
        node.parallel_config.reducer_ref
        for graph in graphs
        for node in graph.nodes
        if getattr(node, "parallel_config", None) is not None
        and node.parallel_config.reducer_ref is not None
    }
    for reference in reducers:
        resolve_reducer_ref(reference)
    return {
        "contracts": _schemas(contracts),
        "graphs": [graph.model_dump(mode="json") for graph in graphs],
        "reducers": sorted(reducers),
    }


def _contracts_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    return {"contracts": _schemas(_contracts(declaration))}


def _service_config_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    config = _load_target(declaration.targets.auth_config)()
    if not isinstance(config, ServiceAuthConfig):
        raise ValueError("auth_config target must return ServiceAuthConfig")
    return {
        "auth_config": config.model_dump(mode="json"),
        "database_backend": get_settings().database.backend,
    }


def _optional_extras_evidence(root: Path, declaration: AppDeclaration) -> dict[str, Any]:
    del root
    targets = [
        *declaration.targets.graph_builders,
        declaration.targets.contracts,
        declaration.targets.auth_config,
        declaration.targets.policy_guard,
        declaration.targets.migration_runner,
    ]
    for reference in targets:
        _load_target(reference)
    return {
        "targets": targets,
        "zeroth_version": importlib.metadata.version("zeroth-core"),
    }


def _policy_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    guard = _load_target(declaration.targets.policy_guard)()
    graphs = _graphs(declaration)
    policy_refs = {
        reference
        for graph in graphs
        for reference in (
            *graph.policy_bindings,
            *(ref for node in graph.nodes for ref in node.policy_bindings),
        )
    }
    capability_refs = {
        reference
        for graph in graphs
        for node in graph.nodes
        for reference in node.capability_bindings
    }
    return {
        "capabilities": {
            ref: guard.capability_registry.resolve(ref).value for ref in sorted(capability_refs)
        },
        "graphs": [graph.model_dump(mode="json") for graph in graphs],
        "policies": {
            ref: guard.policy_registry.resolve(ref).model_dump(mode="json")
            for ref in sorted(policy_refs)
        },
    }


def collect_candidate_evidence(
    name: str,
    root: Path,
    declaration: AppDeclaration,
    *,
    installed_version: str | None = None,
) -> dict[str, Any]:
    """Collect untrusted canonical data in the candidate-only interpreter."""
    if name == "graph":
        return _graph_evidence(declaration)
    if name == "contracts":
        return _contracts_evidence(declaration)
    if name == "service-config":
        return _service_config_evidence(declaration)
    if name == "optional-extras":
        evidence = _optional_extras_evidence(root, declaration)
        if installed_version is not None:
            evidence["zeroth_version"] = installed_version
        return evidence
    if name == "policies":
        return _policy_evidence(declaration)
    raise ValueError(f"no candidate semantic check named {name!r}")


def _target_source_digests(name: str, root: Path, declaration: AppDeclaration) -> dict[str, str]:
    targets = declaration.targets
    references = {
        "graph": [*targets.graph_builders, targets.contracts],
        "contracts": [targets.contracts],
        "service-config": [targets.auth_config],
        "optional-extras": [
            *targets.graph_builders,
            targets.contracts,
            targets.auth_config,
            targets.policy_guard,
            targets.migration_runner,
        ],
        "policies": [*targets.graph_builders, targets.policy_guard],
        "migrations": [targets.migration_runner],
    }[name]
    bindings: dict[str, str] = {}
    for reference in sorted(set(references)):
        module = Path(*reference.partition(":")[0].split("."))
        source = next(
            (
                path
                for path in (root / module.with_suffix(".py"), root / module / "__init__.py")
                if path.is_file()
            ),
            None,
        )
        if source is None:
            raise ValueError(f"declared target source is missing for {reference!r}")
        source = source.resolve()
        source.relative_to(root.resolve())
        bindings[reference] = file_digest(source)
    return bindings


def _payload(
    name: str, evidence: dict[str, Any], root: Path, declaration: AppDeclaration
) -> dict[str, Any]:
    return {
        "check": name,
        "evidence": evidence,
        "schema_version": 1,
        "target_sources": _target_source_digests(name, root, declaration),
    }


def _probe_serialize(name: str, root: Path, declaration: AppDeclaration) -> dict[str, Any]:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        sys.path.insert(0, str(root))
        try:
            evidence = collect_candidate_evidence(name, root, declaration)
        finally:
            sys.path.pop(0)
    return _payload(name, evidence, root, declaration)


def _probe_operate(
    name: str,
    root: Path,
    reference: str,
    database_url: str | None,
) -> dict[str, Any]:
    """Run one target operation; the parent supervisor owns sequencing and validation."""
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        sys.path.insert(0, str(root))
        try:
            if name == "import-target":
                _load_target(reference)
            elif name == "resolve-reducer":
                resolve_reducer_ref(reference)
            elif name == "run-migration":
                runner = _load_target(reference)
                if not callable(runner):
                    raise ValueError("migration_runner target must be callable")
                if database_url is None:
                    raise ValueError("migration operation requires a database URL")
                runner(database_url)
            else:
                raise ValueError(f"unknown candidate operation {name!r}")
        finally:
            sys.path.pop(0)
    return {"operation": name, "reference": reference, "schema_version": 1}


def main(argv: list[str] | None = None) -> int:
    """Run only the untrusted serializer; trusted validation lives elsewhere."""
    parser = argparse.ArgumentParser(prog="python -m release.app_certification.candidate_process")
    parser.add_argument("name")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--declaration-json", required=True)
    parser.add_argument("--reference")
    parser.add_argument("--database-url")
    args = parser.parse_args(argv)
    declaration = AppDeclaration.model_validate_json(args.declaration_json)
    root = args.root.resolve()
    try:
        if args.name in {"import-target", "resolve-reducer", "run-migration"}:
            if args.reference is None:
                parser.error(f"{args.name} requires --reference")
            payload = _probe_operate(args.name, root, args.reference, args.database_url)
        else:
            payload = _probe_serialize(args.name, root, declaration)
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    except Exception as error:  # noqa: BLE001 - untrusted faults become diagnostics
        print(f"{args.name}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
