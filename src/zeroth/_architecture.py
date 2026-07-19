"""Backend dependency policy and repository import scanner."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

BACKEND_DOMAINS = frozenset(
    {
        "contracts",
        "econ",
        "eval",
        "governance",
        "integrations",
        "platform",
        "runtime",
        "service",
    }
)

ALLOWED_DEPENDENCIES = {
    "platform": frozenset(),
    "contracts": frozenset({"platform"}),
    "governance": frozenset({"contracts", "platform"}),
    "runtime": frozenset({"contracts", "governance", "platform"}),
    "econ": frozenset({"contracts", "platform"}),
    "integrations": frozenset({"contracts", "econ", "governance", "platform", "runtime"}),
    "service": BACKEND_DOMAINS - {"service"},
    "eval": frozenset({"contracts", "platform", "runtime"}),
}

LEGACY_DOMAIN_PREFIXES = {
    "zeroth.core.agent_runtime": "runtime",
    "zeroth.core.approvals": "governance",
    "zeroth.core.artifacts": "platform",
    "zeroth.core.audit": "governance",
    "zeroth.core.conditions": "contracts",
    "zeroth.core.config": "platform",
    "zeroth.core.context_window": "runtime",
    "zeroth.core.contracts": "contracts",
    "zeroth.core.deployments": "service",
    "zeroth.core.dispatch": "platform",
    "zeroth.core.econ": "econ",
    "zeroth.core.eval": "eval",
    "zeroth.core.execution_units": "integrations",
    "zeroth.core.governed.app": "contracts",
    "zeroth.core.governed.audit": "governance",
    "zeroth.core.governed.integrations": "integrations",
    "zeroth.core.governed.memory": "integrations",
    "zeroth.core.governed.models": "contracts",
    "zeroth.core.governed.runtime": "runtime",
    "zeroth.core.governed.tools": "runtime",
    "zeroth.core.graph": "contracts",
    "zeroth.core.guardrails": "governance",
    "zeroth.core.http": "integrations",
    "zeroth.core.identity": "governance",
    "zeroth.core.mappings": "contracts",
    "zeroth.core.memory": "integrations",
    "zeroth.core.observability": "platform",
    "zeroth.core.orchestrator": "runtime",
    "zeroth.core.parallel": "runtime",
    "zeroth.core.policy": "governance",
    "zeroth.core.rag": "integrations",
    "zeroth.core.retention": "governance",
    "zeroth.core.runs.repository": "integrations",
    "zeroth.core.runs": "runtime",
    "zeroth.core.sandbox_sidecar": "integrations",
    "zeroth.core.secrets": "platform",
    "zeroth.core.service": "service",
    "zeroth.core.signing": "platform",
    "zeroth.core.storage": "platform",
    "zeroth.core.subgraph": "runtime",
    "zeroth.core.templates": "contracts",
    "zeroth.core.webhooks": "service",
    "zeroth.econ_plane": "econ",
}


@dataclass(frozen=True)
class DependencyViolation:
    """A disallowed import between two classified backend domains."""

    path: Path
    importer: str
    line: int
    imported: str


@dataclass(frozen=True)
class DependencyException:
    """Temporary permission for one exact module-to-module dependency."""

    reason: str
    removal_task: str

    def __post_init__(self) -> None:
        if not self.reason.strip() or not self.removal_task.strip():
            raise ValueError("dependency exceptions require a reason and removal task")


def _exception_group(
    *edges: tuple[str, str], reason: str, removal_task: str
) -> dict[tuple[str, str], DependencyException]:
    return {edge: DependencyException(reason=reason, removal_task=removal_task) for edge in edges}


TEMPORARY_EXCEPTIONS = {
    **_exception_group(
        ("zeroth.core.runs", "zeroth.integrations.persistence.runs"),
        reason=(
            "zeroth.core.runs:RunRepository and :ThreadRepository are protected "
            "legacy capabilities, so the legacy package must keep republishing the "
            "concrete adapters it no longer owns. Resolution stays lazy: making it "
            "eager reintroduces the cycle that blocked the extraction."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.core.graph.validation", "zeroth.runtime.graph_validation"),
        reason=(
            "zeroth.core.graph.validation is a compatibility re-export of the "
            "public GraphValidator, which composes contract validators with "
            "execution checks and therefore lives in the runtime layer. "
            "Resolution stays lazy: an eager import here would put the runtime "
            "on the import path of anything reaching zeroth.core.graph."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.core.orchestrator.runtime", "zeroth.core.execution_units"),
        reason=(
            "RuntimeOrchestrator names ExecutableUnitRunner in its "
            "executable_unit_runner field annotation, and that full __init__ "
            "signature is pinned in the immutable backend_surface_legacy.json. "
            "The type name cannot be renamed or dropped, and the dependency "
            "scanner walks the AST, so even a TYPE_CHECKING-guarded import "
            "still records the edge. Same wall as RepositoryThreadStateStore "
            "in docs/backend-import-migration.md."
        ),
        removal_task="Task 14: move runtime packages and inject owned protocols.",
    ),
    **_exception_group(
        ("zeroth.runtime.orchestration.dispatcher", "zeroth.core.econ.adapter"),
        reason=(
            "NodeDispatcher wraps an agent's provider in InstrumentedProviderAdapter "
            "for per-call cost attribution. Removing the edge needs a wrapping seam "
            "on the injected cost_estimator, but that field is typed 'object | None' "
            "and duck-typed doubles are already relied on (tests/test_econ_adapter.py "
            "passes a bare object), so requiring a new method breaks existing callers. "
            "The construction moved with the dispatch code it belongs to; the edge "
            "itself outlives Task 8."
        ),
        removal_task="Task 14: move economics behind contract and platform boundaries.",
    ),
    **_exception_group(
        (
            "zeroth.runtime.orchestration.dispatcher",
            "zeroth.core.governed.memory.models",
        ),
        reason=(
            "NodeDispatcher maps the authored string scope names on retrieval and "
            "template-memory bindings onto MemoryScope members before calling a "
            "connector. The enum cannot move: its module path is embedded in "
            "signature strings pinned by the immutable legacy fixture, the same "
            "wall documented for policy.models:Capability."
        ),
        removal_task="Task 14: move runtime packages and inject owned protocols.",
    ),
    **_exception_group(
        (
            "zeroth.runtime.orchestration.tool_executor",
            "zeroth.core.execution_units.inline",
        ),
        reason=(
            "RuntimeToolExecutor synthesizes a binding for a Studio code node, whose "
            "source travels in the graph rather than a registry. build_inline_binding "
            "constructs ExecutableUnitBinding and the inline manifest models, so it "
            "cannot move to contracts; removing the edge needs a new run-inline-source "
            "method on ExecutableUnitRunner, which is outside this task's boundary."
        ),
        removal_task="Task 14: move runtime packages and inject owned protocols.",
    ),
    **_exception_group(
        ("zeroth.core.retention.erasure_service", "zeroth.integrations.persistence.runs"),
        reason=(
            "RetentionErasureService's pinned __init__ names RunRepository in the "
            "run_repository annotation, and the dependency scanner walks the AST, "
            "so even the TYPE_CHECKING import records the edge. Narrowing to the "
            "run persistence protocols changes the pinned annotation text -- the "
            "same wall as RepositoryThreadStateStore. Task 9 removed the two "
            "econ_plane edges by moving the concrete eraser into the econ domain; "
            "this one outlives the decomposition and ends with the legacy surface."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.core.config.settings", "zeroth.core.econ.models"),
        ("zeroth.core.config.settings", "zeroth.core.http.models"),
        ("zeroth.core.dispatch.lease", "zeroth.core.runs"),
        ("zeroth.core.dispatch.worker", "zeroth.core.runs"),
        ("zeroth.core.dispatch.worker", "zeroth.core.graph"),
        ("zeroth.core.dispatch.worker", "zeroth.core.guardrails.dead_letter"),
        ("zeroth.core.dispatch.worker", "zeroth.core.orchestrator"),
        ("zeroth.core.dispatch.worker", "zeroth.core.approvals"),
        ("zeroth.core.secrets.provider", "zeroth.core.execution_units.models"),
        ("zeroth.core.storage.redis", "zeroth.core.governed.audit.redis"),
        ("zeroth.core.storage.redis", "zeroth.core.governed.runtime"),
        reason="Legacy infrastructure packages contain domain-aware wiring.",
        removal_task="Task 11: move platform packages and relocate domain-aware wiring.",
    ),
    **_exception_group(
        ("zeroth.core.conditions.branch", "zeroth.core.runs.models"),
        ("zeroth.core.conditions.evaluator", "zeroth.core.runs.models"),
        ("zeroth.core.conditions.models", "zeroth.core.runs.models"),
        ("zeroth.core.conditions.recorder", "zeroth.core.runs.models"),
        ("zeroth.core.contracts.registry", "zeroth.core.governed.tools.base"),
        ("zeroth.core.graph", "zeroth.core.subgraph.models"),
        ("zeroth.core.graph.models", "zeroth.core.context_window.models"),
        ("zeroth.core.graph.models", "zeroth.core.parallel.models"),
        ("zeroth.core.graph.models", "zeroth.core.policy.models"),
        ("zeroth.core.graph.models", "zeroth.core.subgraph.models"),
        reason="Legacy contract packages embed runtime and governance-owned models.",
        removal_task="Task 12: move contract packages and separate runtime-owned types.",
    ),
    **_exception_group(
        ("zeroth.core.approvals.service", "zeroth.core.runs"),
        ("zeroth.core.approvals.service", "zeroth.core.orchestrator.runtime"),
        ("zeroth.core.guardrails.dead_letter", "zeroth.core.runs"),
        (
            "zeroth.core.guardrails.dead_letter",
            "zeroth.integrations.persistence.runs.run_repository",
        ),
        ("zeroth.core.policy.guard", "zeroth.core.runs"),
        reason="Legacy governance services depend directly on runtime implementations.",
        removal_task="Task 13: move governance packages behind contract-owned interfaces.",
    ),
    **_exception_group(
        ("zeroth.core.agent_runtime.factory", "zeroth.core.deployments"),
        (
            "zeroth.core.agent_runtime.thread_store",
            "zeroth.integrations.persistence.runs",
        ),
        (
            "zeroth.core.agent_runtime.provider",
            "zeroth.core.governed.integrations.tool_calls",
        ),
        (
            "zeroth.core.agent_runtime.runner",
            "zeroth.core.governed.integrations.tool_calls",
        ),
        ("zeroth.core.agent_runtime.runner", "zeroth.core.governed.memory.models"),
        ("zeroth.core.agent_runtime.runner", "zeroth.core.memory"),
        ("zeroth.core.subgraph.resolver", "zeroth.core.deployments.models"),
        ("zeroth.core.subgraph.resolver", "zeroth.core.deployments.service"),
        reason="Legacy runtime packages resolve concrete service and integration objects.",
        removal_task="Task 14: move runtime packages and inject owned protocols.",
    ),
    **_exception_group(
        ("zeroth.core.econ.adapter", "zeroth.core.agent_runtime.provider"),
        ("zeroth.core.econ.opportunities", "zeroth.core.audit.models"),
        ("zeroth.core.econ.quality", "zeroth.core.audit.models"),
        ("zeroth.core.econ.quality", "zeroth.core.runs.models"),
        (
            "zeroth.core.econ.rightsizing_experiment",
            "zeroth.core.agent_runtime.provider",
        ),
        ("zeroth.core.econ.rightsizing_experiment", "zeroth.core.audit.models"),
        ("zeroth.core.econ.rightsizing_experiment", "zeroth.core.eval.models"),
        ("zeroth.core.econ.rightsizing_experiment", "zeroth.core.eval.runner"),
        ("zeroth.core.econ.rightsizing_experiment", "zeroth.core.eval.scorers"),
        ("zeroth.core.econ.unit_economics", "zeroth.core.audit.models"),
        ("zeroth.core.econ.unit_economics", "zeroth.core.runs.models"),
        ("zeroth.core.econ.waste", "zeroth.core.audit.models"),
        ("zeroth.core.econ.waste", "zeroth.core.runs"),
        ("zeroth.core.econ.waste", "zeroth.core.runs.models"),
        reason="Legacy economics analytics directly consume runtime and governance data.",
        removal_task="Task 14: move economics behind contract and platform boundaries.",
    ),
}


@dataclass(frozen=True)
class DependencyScan:
    """Complete result of scanning a source tree."""

    scanned_files: tuple[Path, ...]
    violations: tuple[DependencyViolation, ...]


def _module_name(path: Path, source_root: Path) -> tuple[str, bool]:
    relative = path.relative_to(source_root).with_suffix("")
    is_package = relative.name == "__init__"
    parts = relative.parts[:-1] if is_package else relative.parts
    return ".".join(parts), is_package


def _relative_module(
    importer: str,
    *,
    is_package: bool,
    level: int,
    module: str | None,
) -> str:
    package_parts = importer.split(".") if is_package else importer.split(".")[:-1]
    keep = len(package_parts) - (level - 1)
    parts = package_parts[: max(keep, 0)]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _module_index(paths: tuple[Path, ...], source_root: Path) -> frozenset[str]:
    modules: set[str] = set()
    for path in paths:
        module, _ = _module_name(path, source_root)
        parts = module.split(".")
        modules.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    return frozenset(modules)


def _imports(path: Path, source_root: Path, module_index: frozenset[str]) -> list[tuple[int, str]]:
    importer, is_package = _module_name(path, source_root)
    tree = ast.parse(path.read_text(), filename=str(path))
    imported: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (
                node.module or ""
                if node.level == 0
                else _relative_module(
                    importer,
                    is_package=is_package,
                    level=node.level,
                    module=node.module,
                )
            )
            if node.module is not None and base:
                imported.append((node.lineno, base))
            imported.extend(
                (node.lineno, candidate)
                for alias in node.names
                if alias.name != "*"
                and (candidate := f"{base}.{alias.name}".strip(".")) in module_index
            )
    return imported


def _canonical_domain(module: str) -> str | None:
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "zeroth" and parts[1] in BACKEND_DOMAINS:
        return parts[1]
    for prefix in sorted(LEGACY_DOMAIN_PREFIXES, key=len, reverse=True):
        if module == prefix or module.startswith(f"{prefix}."):
            return LEGACY_DOMAIN_PREFIXES[prefix]
    return None


def scan_backend_dependencies(
    source_root: Path,
    *,
    exceptions: dict[tuple[str, str], DependencyException] | None = None,
) -> DependencyScan:
    """Scan every Python file and return all disallowed backend imports."""
    if exceptions is None:
        exceptions = TEMPORARY_EXCEPTIONS
    scanned_files = tuple(sorted((source_root / "zeroth").rglob("*.py")))
    module_index = _module_index(scanned_files, source_root)
    violations: list[DependencyViolation] = []
    for path in scanned_files:
        importer, _ = _module_name(path, source_root)
        imports = _imports(path, source_root, module_index)
        importer_domain = _canonical_domain(importer)
        if importer_domain is None:
            continue
        for line, imported in imports:
            imported_domain = _canonical_domain(imported)
            if imported_domain is None or imported_domain == importer_domain:
                continue
            if imported_domain not in ALLOWED_DEPENDENCIES[importer_domain]:
                if (importer, imported) in exceptions:
                    continue
                violations.append(DependencyViolation(path, importer, line, imported))
    return DependencyScan(scanned_files, tuple(violations))


def find_dependency_violations(
    source_root: Path,
    *,
    exceptions: dict[tuple[str, str], DependencyException] | None = None,
) -> list[DependencyViolation]:
    """Return all disallowed backend imports below ``source_root``."""
    return list(scan_backend_dependencies(source_root, exceptions=exceptions).violations)
