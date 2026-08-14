"""Certifier-owned semantic implementations for every host-side app check."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from zeroth.contracts.graph import Graph
from zeroth.contracts.registry import ContractRegistry, contract_scope_context
from zeroth.governance.policy import PolicyDecision, PolicyGuard
from zeroth.platform.config import get_settings
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.runtime.graph_validation import GraphValidator
from zeroth.service.api.authentication import ServiceAuthConfig
from zeroth.service.bootstrap import run_migrations

from .models import AppDeclaration, load_declaration


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
        model.model_json_schema()
    return contracts


def _graphs(declaration: AppDeclaration) -> list[Graph]:
    graphs: list[Graph] = []
    for reference in declaration.targets.graph_builders:
        builder = _load_target(reference)
        graph = builder()
        if not isinstance(graph, Graph):
            raise ValueError(f"graph builder {reference!r} did not return a Graph")
        graphs.append(graph)
    return graphs


async def _registered_validation(
    contracts: dict[str, type[BaseModel]], graphs: list[Graph]
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
            for name, model in contracts.items():
                await registry.register(model, name=name)
            for graph in graphs:
                await GraphValidator(contract_registry=registry).validate_or_raise(graph)
                await _resolve_graph_contracts(registry, graph)
        finally:
            await database.close()


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
    del root
    asyncio.run(_registered_validation(_contracts(declaration), _graphs(declaration)))


def _check_service_config(root: Path, declaration: AppDeclaration) -> None:
    del root
    settings = get_settings()
    if settings.database.backend not in {"sqlite", "postgresql"}:
        raise ValueError(f"unsupported database backend {settings.database.backend!r}")
    config = _load_target(declaration.targets.auth_config)()
    if not isinstance(config, ServiceAuthConfig):
        raise ValueError("auth_config target must return ServiceAuthConfig")
    if not config.api_keys:
        raise ValueError("ServiceAuthConfig must contain at least one API key")


def _check_contracts(root: Path, declaration: AppDeclaration) -> None:
    del root
    asyncio.run(_registered_validation(_contracts(declaration), []))


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
    app_venv = (root / ".venv").resolve()
    if Path(sys.prefix).resolve() != app_venv:
        raise ValueError(f"optional extras check must run with {app_venv}/bin/python")
    installed = importlib.metadata.version("zeroth-core")
    if installed != declaration.zeroth_version:
        raise ValueError(
            f"installed Zeroth {installed!r} does not match {declaration.zeroth_version!r}"
        )
    for reference in (
        *declaration.targets.graph_builders,
        declaration.targets.contracts,
        declaration.targets.auth_config,
        declaration.targets.policy_guard,
    ):
        _load_target(reference)


def _check_migrations(root: Path, declaration: AppDeclaration) -> None:
    del root, declaration
    with tempfile.TemporaryDirectory(prefix="zeroth-app-migration-") as directory:
        path = Path(directory) / "migration.sqlite"
        run_migrations(f"sqlite:///{path}")
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("migration did not create a non-empty database")


def _container_states(root: Path) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format={{json .State}}",
            "app-cert-packaged",
            "app-cert-ephemeral",
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
    del root
    guard = _load_target(declaration.targets.policy_guard)()
    if not isinstance(guard, PolicyGuard):
        raise ValueError("policy_guard target must return PolicyGuard")
    try:
        bindings = sum(_validate_graph_policies(guard, graph) for graph in _graphs(declaration))
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
    "migrations": _check_migrations,
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
    root = root.resolve()
    sys.path.insert(0, str(root))
    try:
        check(root, declaration)
    finally:
        if sys.path[0] == str(root):
            sys.path.pop(0)


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
    print(f"{args.name}: semantic validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
