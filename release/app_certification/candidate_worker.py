"""Low-privilege candidate evidence collection and trusted finalization."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

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
from .candidate_process import run_importer
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
    name: str, payload: Any, declaration: AppDeclaration, root: Path
) -> None:
    """Independently validate untrusted evidence in the trusted supervisor."""
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


def _importer_argv(name: str, root: Path, declaration: AppDeclaration) -> list[str]:
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
    ]


def _supervise_candidate(name: str, root: Path, declaration: AppDeclaration) -> int:
    returncode, raw, diagnostics = run_importer(_importer_argv(name, root, declaration))
    if returncode:
        detail = diagnostics.strip() or raw.strip() or "candidate importer failed"
        print(f"{name}: trusted finalization unavailable: {detail}", file=sys.stderr)
        return 1
    try:
        payload = json.loads(raw)
        finalize_candidate_evidence(name, payload, declaration, root)
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
    args = parser.parse_args(argv)
    declaration = AppDeclaration.model_validate_json(args.declaration_json)
    root = args.root.resolve()
    return _supervise_candidate(args.name, root, declaration)


if __name__ == "__main__":
    raise SystemExit(main())
