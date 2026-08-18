"""Certifier-owned semantic implementations for every host-side app check."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from zeroth.contracts.graph import Graph
from zeroth.contracts.graph.validation_errors import ValidationCode
from zeroth.contracts.registry import ContractRegistry, contract_scope_context
from zeroth.contracts.registry.schema_model import check_json_schema
from zeroth.governance.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDecision,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.service.api.authentication import ServiceAuthConfig
from zeroth.service.bootstrap import run_migrations

from .models import AppDeclaration, _read_json, file_digest, load_declaration

_RUNTIME_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def candidate_target_references(name: str, declaration: AppDeclaration) -> list[str]:
    """Return the exact declared targets a candidate check must import."""
    targets = declaration.targets
    return {
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


def target_source_digests(name: str, root: Path, declaration: AppDeclaration) -> dict[str, str]:
    """Bind candidate evidence to the declared app modules without importing them."""
    root = root.resolve()
    bindings: dict[str, str] = {}
    for reference in sorted(set(candidate_target_references(name, declaration))):
        module_name = reference.partition(":")[0]
        module = Path(*module_name.split("."))
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
        source.relative_to(root)
        bindings[reference] = file_digest(source)
    return bindings


def _semantic_manifest(root: Path, declaration: AppDeclaration) -> dict[str, Any]:
    candidate = root / declaration.semantic_path
    if candidate.is_symlink():
        raise ValueError("semantic manifest must not be a symlink")
    path = candidate.resolve()
    path.relative_to(root.resolve())
    document = _read_json(path)
    required = {
        "capabilities",
        "contracts",
        "graphs",
        "policies",
        "reducers",
        "schema_version",
        "service_config",
        "target_sources",
        "zeroth_version",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise ValueError("semantic manifest fields do not match schema version 1")
    if document.get("schema_version") != 1:
        raise ValueError("semantic manifest schema_version must be 1")
    return document


def _manifest_sources(
    name: str, root: Path, declaration: AppDeclaration, manifest: dict[str, Any]
) -> None:
    sources = manifest.get("target_sources")
    expected = target_source_digests(name, root, declaration)
    if not isinstance(sources, dict) or any(
        sources.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("semantic manifest does not match the declared target sources")


def _trusted_schemas(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("semantic manifest has no contract schemas")
    schemas: dict[str, dict[str, Any]] = {}
    for name, schema in raw.items():
        if not isinstance(name, str) or not name or not isinstance(schema, dict):
            raise ValueError("semantic manifest contract schemas are malformed")
        try:
            check_json_schema(schema)
        except SchemaError as error:
            raise ValueError(f"semantic manifest contract schema {name!r} is invalid") from error
        schemas[name] = schema
    return schemas


def _trusted_graphs(raw: Any, expected: int) -> list[Graph]:
    if not isinstance(raw, list) or len(raw) != expected:
        raise ValueError("semantic manifest graph count does not match the declaration")
    try:
        return [Graph.model_validate(item) for item in raw]
    except ValidationError as error:
        raise ValueError("semantic manifest Graph is invalid") from error


async def _registered_validation(
    schemas: Mapping[str, dict[str, Any]],
    graphs: list[Graph],
    validated_reducers: frozenset[str],
) -> None:
    with tempfile.TemporaryDirectory(prefix="zeroth-app-cert-") as directory:
        database_path = Path(directory) / "certification.sqlite"
        run_migrations(f"sqlite:///{database_path}")
        database = AsyncSQLiteDatabase(path=str(database_path))
        registry = ContractRegistry.scoped(
            database,
            contract_scope_context("app-certification", None),
        )
        try:
            for name, schema in schemas.items():
                await registry.register_schema(name, schema)
            for graph in graphs:
                report = await GraphValidator(contract_registry=registry).validate(graph)
                errors = [
                    issue
                    for issue in report.errors
                    if not (
                        issue.code is ValidationCode.INVALID_REDUCER_REF
                        and issue.details.get("reducer_ref") in validated_reducers
                    )
                ]
                if errors:
                    details = "; ".join(issue.message for issue in errors)
                    raise ValueError(f"graph validation failed: {details}")
                await _resolve_graph_contracts(registry, graph)
        finally:
            await database.close()


async def validate_serialized_graphs(
    schemas: Mapping[str, dict[str, Any]],
    graphs: list[Graph],
    *,
    validated_reducers: frozenset[str] = frozenset(),
) -> None:
    """Run the complete public graph validator over trusted canonical JSON."""
    await _registered_validation(schemas, graphs, validated_reducers)


async def _resolve_graph_contracts(registry: ContractRegistry, graph: Graph) -> None:
    references = {
        reference
        for node in graph.nodes
        for reference in (node.input_contract_ref, node.output_contract_ref)
        if reference
    }
    for reference in sorted(references):
        try:
            await registry.get(reference)
        except Exception as error:
            raise ValueError(
                f"graph {graph.graph_id!r} references unregistered contract {reference!r}"
            ) from error


def _check_graph(root: Path, declaration: AppDeclaration) -> None:
    manifest = _semantic_manifest(root, declaration)
    _manifest_sources("graph", root, declaration, manifest)
    schemas = _trusted_schemas(manifest["contracts"])
    graphs = _trusted_graphs(manifest["graphs"], len(declaration.targets.graph_builders))
    reducers = manifest["reducers"]
    if reducers not in ([], None):
        raise ValueError("dynamic reducer code is outside the static certification contract")
    asyncio.run(validate_serialized_graphs(schemas, graphs))


def _check_service_config(root: Path, declaration: AppDeclaration) -> None:
    manifest = _semantic_manifest(root, declaration)
    _manifest_sources("service-config", root, declaration, manifest)
    validate_serialized_service_config(manifest["service_config"])


def validated_database_backend(root: Path, declaration: AppDeclaration) -> str:
    """Return the backend selected by the validated semantic manifest."""
    manifest = _semantic_manifest(root, declaration)
    service_config = manifest["service_config"]
    validate_serialized_service_config(service_config)
    return service_config["database_backend"]


def validate_serialized_service_config(evidence: Any) -> ServiceAuthConfig:
    """Validate certifier-owned service configuration JSON."""
    if not isinstance(evidence, dict) or evidence.get("database_backend") not in {
        "sqlite",
        "postgres",
    }:
        raise ValueError("semantic manifest database backend is unsupported")
    config = ServiceAuthConfig.model_validate(evidence.get("auth_config"))
    if not config.api_keys:
        raise ValueError("semantic manifest auth configuration has no API keys")
    return config


def validated_runtime_settings(root: Path, declaration: AppDeclaration) -> dict[str, str]:
    """Return shell-safe runtime settings from the trusted semantic contract."""
    manifest = _semantic_manifest(root, declaration)
    _manifest_sources("service-config", root, declaration, manifest)
    service_config = manifest["service_config"]
    auth_config = validate_serialized_service_config(service_config)
    tenants = {credential.tenant_id for credential in auth_config.api_keys}
    if len(tenants) != 1:
        raise ValueError("semantic manifest auth configuration must use one tenant")
    tenant = tenants.pop()
    if _RUNTIME_TENANT.fullmatch(tenant) is None:
        raise ValueError("semantic manifest tenant must use lowercase letters, digits, or hyphens")
    return {"database_backend": service_config["database_backend"], "runtime_tenant": tenant}


def _check_contracts(root: Path, declaration: AppDeclaration) -> None:
    manifest = _semantic_manifest(root, declaration)
    _manifest_sources("contracts", root, declaration, manifest)
    schemas = _trusted_schemas(manifest["contracts"])
    asyncio.run(validate_serialized_graphs(schemas, []))


def _run(argv: list[str], root: Path, timeout: int) -> None:
    try:
        result = subprocess.run(
            argv,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"owned check timed out after {timeout}s") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no command output"
        raise ValueError(f"owned command exited {result.returncode}: {detail[-500:]}")


def _check_dependency_lock(root: Path, declaration: AppDeclaration) -> None:
    lock = (root / declaration.lock_path).resolve()
    if not lock.is_file() or root.resolve() not in lock.parents:
        raise ValueError(f"dependency lock {declaration.lock_path!r} is missing")
    _run(["uv", "lock", "--check"], root, 120)


def _check_optional_extras(root: Path, declaration: AppDeclaration) -> None:
    manifest = _semantic_manifest(root, declaration)
    _manifest_sources("optional-extras", root, declaration, manifest)
    if manifest.get("zeroth_version") != declaration.zeroth_version:
        raise ValueError("semantic manifest Zeroth version does not match the declaration")


def _container_states(root: Path) -> list[dict[str, Any]]:
    containers = (
        os.environ.get("PACKAGED_CONTAINER", "app-cert-packaged"),
        os.environ.get("EPHEMERAL_CONTAINER", "app-cert-ephemeral"),
    )
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format={{json .State}}",
            *containers,
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        timeout=20,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise ValueError(f"docker inspect failed: {detail}")
    try:
        states = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise ValueError("docker inspect returned malformed state JSON") from error
    if len(states) != 2:
        raise ValueError("docker inspect did not return both candidate containers")
    return states


def _check_container_startup(root: Path, declaration: AppDeclaration) -> None:
    del declaration
    if any(state.get("Running") is not True for state in _container_states(root)):
        raise ValueError("candidate container is not running")


def _check_health(root: Path, declaration: AppDeclaration) -> None:
    del declaration
    states = _container_states(root)
    if any(state.get("Health", {}).get("Status") != "healthy" for state in states):
        raise ValueError("candidate container health status is not healthy")


def _validate_graph_policies(guard: PolicyGuard, graph: Graph) -> int:
    bindings = 0
    for reference in graph.policy_bindings:
        guard.policy_registry.resolve(reference)
        bindings += 1
    for node in graph.nodes:
        for reference in node.policy_bindings:
            guard.policy_registry.resolve(reference)
            bindings += 1
        for reference in node.capability_bindings:
            guard.capability_registry.resolve(reference)
        if node.policy_bindings or node.capability_bindings:
            result = guard.evaluate(graph, node, object(), {})
            if result.decision is not PolicyDecision.ALLOW:
                raise ValueError(f"policy rejected graph {graph.graph_id!r} node {node.node_id!r}")
    return bindings


def _check_policies(root: Path, declaration: AppDeclaration) -> None:
    manifest = _semantic_manifest(root, declaration)
    _manifest_sources("policies", root, declaration, manifest)
    graphs = _trusted_graphs(manifest["graphs"], len(declaration.targets.graph_builders))
    policies, capabilities = manifest["policies"], manifest["capabilities"]
    if not isinstance(policies, dict) or not isinstance(capabilities, dict):
        raise ValueError("semantic manifest policies or capabilities are malformed")
    policy_registry, capability_registry = PolicyRegistry(), CapabilityRegistry()
    for reference, raw in policies.items():
        policy = PolicyDefinition.model_validate(raw)
        if policy.policy_id != reference:
            raise ValueError("semantic manifest policy identifier does not match its binding")
        policy_registry.register(policy)
    for reference, raw in capabilities.items():
        capability_registry.register(reference, Capability(raw))
    guard = PolicyGuard(policy_registry=policy_registry, capability_registry=capability_registry)
    try:
        bindings = sum(_validate_graph_policies(guard, graph) for graph in graphs)
    except KeyError as error:
        raise ValueError(f"policy or capability reference is not registered: {error}") from error
    if not bindings:
        raise ValueError("app graphs contain no registered policy bindings")


def _check_frontend_api(root: Path, declaration: AppDeclaration) -> None:
    checker = Path(__file__).parents[2] / "scripts/check_frontend_api.py"
    _run([sys.executable, str(checker), "--frontend", declaration.targets.frontend_path], root, 120)


_CHECKS = {
    "graph": _check_graph,
    "service-config": _check_service_config,
    "contracts": _check_contracts,
    "dependency-lock": _check_dependency_lock,
    "optional-extras": _check_optional_extras,
    "container-startup": _check_container_startup,
    "health": _check_health,
    "policies": _check_policies,
    "frontend-api": _check_frontend_api,
}


def run_owned_check(name: str, root: Path, declaration: AppDeclaration) -> None:
    """Run one named semantic check whose implementation is certifier-owned."""
    check = _CHECKS.get(name)
    if check is None:
        raise ValueError(f"no certifier-owned host check named {name!r}")
    check(root.resolve(), declaration)


def main(argv: list[str] | None = None) -> int:
    """Execute one owned check in a bounded subprocess."""
    parser = argparse.ArgumentParser(prog="python -m release.app_certification.checks")
    parser.add_argument("name", choices=sorted(_CHECKS))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--declaration", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        run_owned_check(args.name, args.root, load_declaration(args.declaration))
    except Exception as error:  # noqa: BLE001 - boundary emits a concise retained diagnostic
        print(f"{args.name}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"check": args.name, "schema_version": 1, "status": "passed"},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
