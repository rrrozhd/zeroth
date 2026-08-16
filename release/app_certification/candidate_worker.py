"""Low-privilege candidate evidence collection and trusted finalization."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from zeroth.contracts.graph import Graph
from zeroth.contracts.graph.validation import ContractValidator
from zeroth.contracts.graph.validation_errors import GraphValidationReport
from zeroth.contracts.registry.schema_model import check_json_schema
from zeroth.governance.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.service.api.authentication import ServiceAuthConfig

from . import checks
from .models import AppDeclaration

CANDIDATE_CHECKS = frozenset(
    {"graph", "service-config", "contracts", "optional-extras", "policies"}
)
_IMPORT_BOOTSTRAP = (
    "import pathlib,runpy,sys;"
    "certifier=pathlib.Path(sys.argv.pop(1));"
    "venv=pathlib.Path(sys.argv.pop(1));"
    "site_packages=venv/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/"
    "'site-packages';"
    "sys.prefix=sys.exec_prefix=str(venv);"
    "sys.path[:0]=[str(certifier),str(certifier/'src'),str(site_packages)];"
    "runpy.run_module('release.app_certification.candidate_worker',run_name='__main__')"
)


def _schemas(contracts: Mapping[str, type[BaseModel]]) -> dict[str, dict[str, Any]]:
    return {name: model.model_json_schema() for name, model in contracts.items()}


def _graph_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    contracts = checks._contracts(declaration)
    graphs = checks._graphs(declaration)
    asyncio.run(checks._registered_validation(contracts, graphs))
    reducers = {
        node.parallel_config.reducer_ref
        for graph in graphs
        for node in graph.nodes
        if getattr(node, "parallel_config", None) is not None
        and node.parallel_config.reducer_ref is not None
    }
    return {
        "contracts": _schemas(contracts),
        "graphs": [graph.model_dump(mode="json") for graph in graphs],
        "reducers": sorted(reducers),
    }


def _contracts_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    contracts = checks._contracts(declaration)
    asyncio.run(checks._registered_validation(contracts, []))
    return {"contracts": _schemas(contracts)}


def _service_config_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    settings = checks.get_settings()
    if settings.database.backend not in {"sqlite", "postgresql"}:
        raise ValueError(f"unsupported database backend {settings.database.backend!r}")
    config = checks._load_target(declaration.targets.auth_config)()
    if not isinstance(config, ServiceAuthConfig):
        raise ValueError("auth_config target must return ServiceAuthConfig")
    if not config.api_keys:
        raise ValueError("ServiceAuthConfig must contain at least one API key")
    return {
        "auth_config": config.model_dump(mode="json"),
        "database_backend": settings.database.backend,
    }


def _optional_extras_evidence(
    root: Path, declaration: AppDeclaration, installed_version: str | None
) -> dict[str, Any]:
    app_venv = (root / ".venv").resolve()
    if Path(sys.prefix).resolve() != app_venv and installed_version is None:
        raise ValueError(f"optional extras check must run with {app_venv}/bin/python")
    targets = [
        *declaration.targets.graph_builders,
        declaration.targets.contracts,
        declaration.targets.auth_config,
        declaration.targets.policy_guard,
    ]
    for reference in targets:
        checks._load_target(reference)
    return {
        "targets": targets,
        "zeroth_version": installed_version or importlib.metadata.version("zeroth-core"),
    }


def _policy_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    guard = checks._load_target(declaration.targets.policy_guard)()
    if not isinstance(guard, PolicyGuard):
        raise ValueError("policy_guard target must return PolicyGuard")
    graphs = checks._graphs(declaration)
    if not sum(checks._validate_graph_policies(guard, graph) for graph in graphs):
        raise ValueError("app graphs contain no registered policy bindings")
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
    """Collect provisional semantic evidence in the candidate process."""
    if name == "graph":
        return _graph_evidence(declaration)
    if name == "contracts":
        return _contracts_evidence(declaration)
    if name == "service-config":
        return _service_config_evidence(declaration)
    if name == "optional-extras":
        return _optional_extras_evidence(root, declaration, installed_version)
    if name == "policies":
        return _policy_evidence(declaration)
    raise ValueError(f"no candidate semantic check named {name!r}")


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
    validator = ContractValidator(capability_checks=GraphValidator())
    expected_reducers: set[str] = set()
    for graph in graphs:
        issues = []
        validator.validate(graph, issues)
        GraphValidationReport(graph_id=graph.graph_id, issues=issues).raise_for_errors()
        for node in graph.nodes:
            for reference in (node.input_contract_ref, node.output_contract_ref):
                if reference and reference not in schemas:
                    raise ValueError(f"graph references unregistered contract {reference!r}")
            config = getattr(node, "parallel_config", None)
            if config is not None and config.reducer_ref is not None:
                expected_reducers.add(config.reducer_ref)
            if config is not None and config.merge_strategy == "merge":
                schema = schemas.get(node.output_contract_ref or "", {})
                if schema.get("type") != "object":
                    raise ValueError("merge output contract must have top-level object type")
    if reducers != sorted(expected_reducers):
        raise ValueError("candidate reducer evidence does not match the graphs")


def _finalize_graph(evidence: dict[str, Any], declaration: AppDeclaration) -> None:
    schemas = _trusted_schemas(evidence.get("contracts"))
    graphs = _trusted_graphs(evidence.get("graphs"), len(declaration.targets.graph_builders))
    _validate_static_graphs(graphs, schemas, evidence.get("reducers"))


def _finalize_contracts(evidence: dict[str, Any]) -> None:
    _trusted_schemas(evidence.get("contracts"))


def _finalize_service_config(evidence: dict[str, Any]) -> None:
    if evidence.get("database_backend") not in {"sqlite", "postgresql"}:
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


def finalize_candidate_evidence(
    name: str, payload: Any, declaration: AppDeclaration
) -> None:
    """Independently validate untrusted evidence in the trusted supervisor."""
    if not isinstance(payload, dict) or set(payload) != {"check", "evidence", "schema_version"}:
        raise ValueError("candidate evidence has no trusted finalization payload")
    if payload.get("check") != name or payload.get("schema_version") != 1:
        raise ValueError("candidate evidence does not match the requested check")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("candidate semantic evidence is malformed")
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
    else:
        raise ValueError(f"no candidate semantic check named {name!r}")


def _payload(name: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"check": name, "evidence": evidence, "schema_version": 1}


def _import_candidate(
    name: str, root: Path, declaration: AppDeclaration, result_fd: int
) -> int:
    sys.path.insert(0, str(root))
    try:
        evidence = collect_candidate_evidence(name, root, declaration)
        encoded = json.dumps(_payload(name, evidence)).encode()
        with os.fdopen(os.dup(result_fd), "wb") as result:
            result.write(encoded)
    except Exception as error:  # noqa: BLE001 - importer returns one captured diagnostic
        print(f"{name}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        if sys.path[0] == str(root):
            sys.path.pop(0)
    return 0


def _importer_argv(
    name: str, root: Path, declaration: AppDeclaration, result_fd: int
) -> list[str]:
    return [
        str(Path(sys.executable).absolute()),
        "-I",
        "-S",
        "-c",
        _IMPORT_BOOTSTRAP,
        str(Path(__file__).parents[2].resolve()),
        str(Path(sys.prefix).resolve()),
        name,
        "--root",
        str(root),
        "--declaration-json",
        declaration.model_dump_json(),
        "--result-fd",
        str(result_fd),
    ]


def _supervise_candidate(name: str, root: Path, declaration: AppDeclaration) -> int:
    with tempfile.TemporaryFile() as provisional:
        process = subprocess.Popen(
            _importer_argv(name, root, declaration, provisional.fileno()),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(provisional.fileno(),),
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=150)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            detail = stderr.strip() or stdout.strip() or "candidate importer timed out"
            print(f"{name}: trusted finalization unavailable: {detail}", file=sys.stderr)
            return 1
        provisional.seek(0)
        raw = provisional.read()
    if process.returncode:
        detail = stderr.strip() or stdout.strip() or "candidate importer failed"
        print(f"{name}: trusted finalization unavailable: {detail}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw)
        finalize_candidate_evidence(name, payload, declaration)
    except Exception as error:  # noqa: BLE001 - provisional evidence is untrusted
        detail = f"{name}: trusted finalization rejected provisional evidence: {error}"
        print(detail, file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Supervise candidate import without sharing the authoritative stdout."""
    parser = argparse.ArgumentParser(prog="python -m release.app_certification.candidate_worker")
    parser.add_argument("name", choices=sorted(CANDIDATE_CHECKS))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--declaration-json", required=True)
    parser.add_argument("--result-fd", type=int)
    args = parser.parse_args(argv)
    declaration = AppDeclaration.model_validate_json(args.declaration_json)
    root = args.root.resolve()
    if args.result_fd is not None:
        return _import_candidate(args.name, root, declaration, args.result_fd)
    return _supervise_candidate(args.name, root, declaration)


if __name__ == "__main__":
    raise SystemExit(main())
