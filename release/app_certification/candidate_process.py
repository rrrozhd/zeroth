"""Serialize candidate objects without loading trusted finalizer state."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from zeroth.platform.config import get_settings

from .models import AppDeclaration, file_digest

_OUTPUT_LIMIT = 1 << 20


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
    return dict(value)


def _graphs(declaration: AppDeclaration) -> list[Any]:
    return [_load_target(reference)() for reference in declaration.targets.graph_builders]


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
    return {
        "contracts": _schemas(contracts),
        "graphs": [graph.model_dump(mode="json") for graph in graphs],
        "reducers": sorted(reducers),
    }


def _contracts_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    return {"contracts": _schemas(_contracts(declaration))}


def _service_config_evidence(declaration: AppDeclaration) -> dict[str, Any]:
    return {
        "auth_config": _load_target(declaration.targets.auth_config)().model_dump(mode="json"),
        "database_backend": get_settings().database.backend,
    }


def _optional_extras_evidence(root: Path, declaration: AppDeclaration) -> dict[str, Any]:
    del root
    targets = [
        *declaration.targets.graph_builders,
        declaration.targets.contracts,
        declaration.targets.auth_config,
        declaration.targets.policy_guard,
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


def _target_source_digests(
    name: str, root: Path, declaration: AppDeclaration
) -> dict[str, str]:
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
        ],
        "policies": [*targets.graph_builders, targets.policy_guard],
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


def _limit_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (_OUTPUT_LIMIT, _OUTPUT_LIMIT))


def run_importer(argv: list[str]) -> tuple[int, str, str]:
    """Run one candidate-only serializer with bounded output and lifetime."""
    with tempfile.TemporaryFile(mode="w+") as stdout, tempfile.TemporaryFile(
        mode="w+"
    ) as stderr:
        process = subprocess.Popen(
            argv,
            stdout=stdout,
            stderr=stderr,
            text=True,
            preexec_fn=_limit_output,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.wait(timeout=150)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        stdout.seek(0)
        stderr.seek(0)
        output, diagnostics = stdout.read(_OUTPUT_LIMIT + 1), stderr.read(_OUTPUT_LIMIT + 1)
    if timed_out:
        return 1, output, diagnostics or "candidate serializer timed out"
    if len(output) > _OUTPUT_LIMIT or len(diagnostics) > _OUTPUT_LIMIT:
        return 1, "", "candidate output exceeded limit"
    return process.returncode, output, diagnostics


def _serialize(name: str, root: Path, declaration: AppDeclaration) -> int:
    sys.path.insert(0, str(root))
    saved_stdout, saved_stderr = os.dup(1), os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    try:
        evidence = collect_candidate_evidence(name, root, declaration)
    except Exception as error:  # noqa: BLE001 - untrusted failure becomes one diagnostic
        outcome: tuple[int, Any] = (1, error)
    else:
        outcome = (0, _payload(name, evidence, root, declaration))
    finally:
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)
        os.close(devnull)
        sys.path.pop(0)
    if outcome[0]:
        error = outcome[1]
        print(f"{name}: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(outcome[1], sort_keys=True, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run only the untrusted serializer; trusted validation lives elsewhere."""
    parser = argparse.ArgumentParser(prog="python -m release.app_certification.candidate_process")
    parser.add_argument("name")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--declaration-json", required=True)
    args = parser.parse_args(argv)
    declaration = AppDeclaration.model_validate_json(args.declaration_json)
    return _serialize(args.name, args.root.resolve(), declaration)


if __name__ == "__main__":
    raise SystemExit(main())
