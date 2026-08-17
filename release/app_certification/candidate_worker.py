"""Low-privilege candidate evidence collection and trusted finalization."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import re
import sys
from pathlib import Path
from typing import Any

from zeroth.contracts.graph import Graph
from zeroth.contracts.registry.schema_model import check_json_schema
from zeroth.governance.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.service.api.authentication import ServiceAuthConfig

from . import checks
from .candidate_process import run_importer
from .migration_supervisor import inspect_migration
from .models import AppDeclaration

CANDIDATE_CHECKS = frozenset(
    {"graph", "service-config", "contracts", "optional-extras", "policies", "migrations"}
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMPORT_BOOTSTRAP = (
    "import pathlib,runpy,sys;"
    "certifier=pathlib.Path(sys.argv.pop(1));"
    "venv=pathlib.Path(sys.argv.pop(1));"
    "site_packages=venv/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/"
    "'site-packages';"
    "sys.prefix=sys.exec_prefix=str(venv);"
    "sys.path[:0]=[str(certifier),str(certifier/'src'),str(site_packages)];"
    "runpy.run_module('release.app_certification.candidate_process',run_name='__main__')"
)


def _trusted_schemas(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("candidate evidence has no contract schemas")
    schemas: dict[str, dict[str, Any]] = {}
    for name, schema in raw.items():
        if not isinstance(name, str) or not name or not isinstance(schema, dict):
            raise ValueError("candidate contract evidence is malformed")
        check_json_schema(schema)
        schemas[name] = schema
    return schemas


def _trusted_graphs(raw: Any, expected: int) -> list[Graph]:
    if not isinstance(raw, list) or len(raw) != expected:
        raise ValueError("candidate graph evidence count does not match the declaration")
    return [Graph.model_validate(item) for item in raw]


def _validate_static_graphs(
    graphs: list[Graph], schemas: dict[str, dict[str, Any]], reducers: Any
) -> None:
    expected_reducers = _graph_reducers(graphs)
    if reducers != sorted(expected_reducers):
        raise ValueError("candidate reducer evidence does not match the graphs")
    asyncio.run(
        checks.validate_serialized_graphs(
            schemas,
            graphs,
            validated_reducers=frozenset(expected_reducers),
        )
    )


def _graph_reducers(graphs: list[Graph]) -> set[str]:
    return {
        config.reducer_ref
        for graph in graphs
        for node in graph.nodes
        if (config := getattr(node, "parallel_config", None)) is not None
        and config.reducer_ref is not None
    }


def _finalize_graph(evidence: dict[str, Any], declaration: AppDeclaration) -> None:
    schemas = _trusted_schemas(evidence.get("contracts"))
    graphs = _trusted_graphs(evidence.get("graphs"), len(declaration.targets.graph_builders))
    _validate_static_graphs(graphs, schemas, evidence.get("reducers"))


def _finalize_contracts(evidence: dict[str, Any]) -> None:
    _trusted_schemas(evidence.get("contracts"))


def _finalize_service_config(evidence: dict[str, Any]) -> None:
    if evidence.get("database_backend") not in {"sqlite", "postgres"}:
        raise ValueError("candidate database backend evidence is unsupported")
    config = ServiceAuthConfig.model_validate(evidence.get("auth_config"))
    if not config.api_keys:
        raise ValueError("candidate auth configuration has no API keys")


def _finalize_optional_extras(evidence: dict[str, Any], declaration: AppDeclaration) -> None:
    expected = [
        *declaration.targets.graph_builders,
        declaration.targets.contracts,
        declaration.targets.auth_config,
        declaration.targets.policy_guard,
        declaration.targets.migration_runner,
    ]
    if evidence.get("targets") != expected:
        raise ValueError("candidate imported-target evidence does not match the declaration")
    if evidence.get("zeroth_version") != declaration.zeroth_version:
        raise ValueError("candidate installed Zeroth evidence does not match the declaration")


def _finalize_policies(evidence: dict[str, Any], declaration: AppDeclaration) -> None:
    graphs = _trusted_graphs(evidence.get("graphs"), len(declaration.targets.graph_builders))
    policy_refs = {
        ref
        for graph in graphs
        for ref in (
            *graph.policy_bindings,
            *(ref for node in graph.nodes for ref in node.policy_bindings),
        )
    }
    capability_refs = {
        ref for graph in graphs for node in graph.nodes for ref in node.capability_bindings
    }
    policies = evidence.get("policies")
    capabilities = evidence.get("capabilities")
    if not isinstance(policies, dict) or set(policies) != policy_refs:
        raise ValueError("candidate policy evidence does not match graph bindings")
    if not isinstance(capabilities, dict) or set(capabilities) != capability_refs:
        raise ValueError("candidate capability evidence does not match graph bindings")
    policy_registry, capability_registry = PolicyRegistry(), CapabilityRegistry()
    for ref, raw in policies.items():
        policy = PolicyDefinition.model_validate(raw)
        if policy.policy_id != ref:
            raise ValueError("candidate policy identifier does not match its binding")
        policy_registry.register(policy)
    for ref, raw in capabilities.items():
        capability_registry.register(ref, Capability(raw))
    guard = PolicyGuard(policy_registry=policy_registry, capability_registry=capability_registry)
    if not sum(checks._validate_graph_policies(guard, graph) for graph in graphs):
        raise ValueError("trusted policy finalization found no policy bindings")


def _finalize_migrations(evidence: dict[str, Any], declaration: AppDeclaration) -> None:
    if set(evidence) != {"backend", "object_count", "runner", "schema_sha256"}:
        raise ValueError("candidate migration evidence is malformed")
    if evidence.get("backend") not in {"sqlite", "postgres"}:
        raise ValueError("candidate migration backend evidence is unsupported")
    if evidence.get("runner") != declaration.targets.migration_runner:
        raise ValueError("candidate migration runner does not match the declaration")
    if not isinstance(evidence.get("object_count"), int) or evidence["object_count"] <= 0:
        raise ValueError("candidate migration schema is empty")
    if (
        not isinstance(evidence.get("schema_sha256"), str)
        or _DIGEST.fullmatch(evidence["schema_sha256"]) is None
    ):
        raise ValueError("candidate migration schema digest is invalid")


def _trusted_evidence(
    name: str, payload: Any, declaration: AppDeclaration, root: Path
) -> dict[str, Any]:
    expected_keys = {"check", "evidence", "schema_version", "target_sources"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ValueError("candidate evidence has no trusted finalization payload")
    if payload.get("check") != name or payload.get("schema_version") != 1:
        raise ValueError("candidate evidence does not match the requested check")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("candidate semantic evidence is malformed")
    if payload["target_sources"] != checks.target_source_digests(name, root, declaration):
        raise ValueError("candidate evidence does not match the declared target sources")
    return evidence


def finalize_candidate_evidence(
    name: str, payload: Any, declaration: AppDeclaration, root: Path
) -> None:
    """Independently validate untrusted evidence in the trusted supervisor."""
    evidence = _trusted_evidence(name, payload, declaration, root)
    if name == "graph":
        _finalize_graph(evidence, declaration)
    elif name == "contracts":
        _finalize_contracts(evidence)
    elif name == "service-config":
        _finalize_service_config(evidence)
    elif name == "optional-extras":
        _finalize_optional_extras(evidence, declaration)
    elif name == "policies":
        _finalize_policies(evidence, declaration)
    elif name == "migrations":
        _finalize_migrations(evidence, declaration)
    else:
        raise ValueError(f"no candidate semantic check named {name!r}")


def _candidate_argv(
    name: str,
    root: Path,
    declaration: AppDeclaration,
    candidate_venv: Path,
    *extra: str,
) -> list[str]:
    return [
        str(Path(sys.executable).absolute()),
        "-I",
        "-S",
        "-c",
        _IMPORT_BOOTSTRAP,
        str(Path(__file__).parents[2].resolve()),
        str(candidate_venv.resolve()),
        name,
        "--root",
        str(root),
        "--declaration-json",
        declaration.model_dump_json(),
        *extra,
    ]


def _candidate_payload(argv: list[str]) -> Any:
    returncode, raw, diagnostics = run_importer(argv)
    if returncode:
        detail = diagnostics.strip() or raw.strip() or "candidate operation failed"
        raise ValueError(detail)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("candidate operation returned malformed JSON") from error


def _run_operation(
    operation: str,
    reference: str,
    root: Path,
    declaration: AppDeclaration,
    candidate_venv: Path,
    *,
    database_url: str | None = None,
) -> None:
    extra = ["--reference", reference]
    if database_url is not None:
        extra.extend(("--database-url", database_url))
    payload = _candidate_payload(
        _candidate_argv(operation, root, declaration, candidate_venv, *extra)
    )
    expected = {"operation": operation, "reference": reference, "schema_version": 1}
    if payload != expected:
        raise ValueError(f"candidate {operation} result is malformed")


def _supervised_payload(
    name: str, evidence: dict[str, Any], root: Path, declaration: AppDeclaration
) -> dict[str, Any]:
    return {
        "check": name,
        "evidence": evidence,
        "schema_version": 1,
        "target_sources": checks.target_source_digests(name, root, declaration),
    }


def _candidate_version(candidate_venv: Path) -> str:
    site_packages = (
        candidate_venv
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    for distribution in importlib.metadata.distributions(path=[str(site_packages)]):
        name = distribution.metadata.get("Name", "").lower().replace("_", "-")
        if name == "zeroth-core":
            return distribution.version
    raise ValueError("candidate environment has no zeroth-core distribution")


def _supervise_optional_imports(
    root: Path, declaration: AppDeclaration, candidate_venv: Path
) -> dict[str, Any]:
    targets = checks.candidate_target_references("optional-extras", declaration)
    for reference in dict.fromkeys(targets):
        _run_operation("import-target", reference, root, declaration, candidate_venv)
    return _supervised_payload(
        "optional-extras",
        {"targets": targets, "zeroth_version": _candidate_version(candidate_venv)},
        root,
        declaration,
    )


def _supervise_reducers(
    payload: Any, root: Path, declaration: AppDeclaration, candidate_venv: Path
) -> None:
    evidence = _trusted_evidence("graph", payload, declaration, root)
    graphs = _trusted_graphs(evidence.get("graphs"), len(declaration.targets.graph_builders))
    reducers = sorted(_graph_reducers(graphs))
    for reference in reducers:
        _run_operation("resolve-reducer", reference, root, declaration, candidate_venv)
    evidence["reducers"] = reducers


def _supervise_migration(
    root: Path, declaration: AppDeclaration, candidate_venv: Path
) -> dict[str, Any]:
    def run_candidate(reference: str, database_url: str) -> None:
        _run_operation(
            "run-migration",
            reference,
            root,
            declaration,
            candidate_venv,
            database_url=database_url,
        )

    return _supervised_payload(
        "migrations", inspect_migration(declaration, run_candidate), root, declaration
    )


def _supervise_candidate(
    name: str, root: Path, declaration: AppDeclaration, candidate_venv: Path
) -> int:
    try:
        if name == "migrations":
            payload = _supervise_migration(root, declaration, candidate_venv)
        elif name == "optional-extras":
            payload = _supervise_optional_imports(root, declaration, candidate_venv)
        else:
            payload = _candidate_payload(_candidate_argv(name, root, declaration, candidate_venv))
            if name == "graph":
                _supervise_reducers(payload, root, declaration, candidate_venv)
        finalize_candidate_evidence(name, payload, declaration, root)
    except Exception as error:  # noqa: BLE001 - candidate evidence is untrusted
        print(f"{name}: trusted finalization failed in supervisor: {error}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Supervise candidate import without sharing the authoritative stdout."""
    parser = argparse.ArgumentParser(prog="python -m release.app_certification.candidate_worker")
    parser.add_argument("name", choices=sorted(CANDIDATE_CHECKS))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--declaration-json", required=True)
    parser.add_argument("--candidate-venv", type=Path, required=True)
    args = parser.parse_args(argv)
    declaration = AppDeclaration.model_validate_json(args.declaration_json)
    root = args.root.resolve()
    return _supervise_candidate(args.name, root, declaration, args.candidate_venv)


if __name__ == "__main__":
    raise SystemExit(main())
