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
        ("zeroth.runtime.orchestration.dispatcher", "zeroth.econ.analytics.adapter"),
        reason=(
            "NodeDispatcher wraps an agent's provider in InstrumentedProviderAdapter "
            "for per-call cost attribution. Removing the edge needs a wrapping seam "
            "on the injected cost_estimator, but that field is typed 'object | None' "
            "and duck-typed doubles are already relied on (tests/test_econ_adapter.py "
            "passes a bare object), so requiring a new method breaks existing callers. "
            "The construction moved with the dispatch code it belongs to; the edge "
            "itself outlives Task 8."
        ),
        removal_task="Task 18: final migration cleanup and verification.",
    ),
    **_exception_group(
        ("zeroth.governance.retention.erasure_service", "zeroth.integrations.persistence.runs"),
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
        ("zeroth.platform.secrets.provider", "zeroth.integrations.execution.models"),
        reason=(
            "SecretResolver's pinned legacy signatures name EnvironmentVariable "
            "in their annotations, and the dependency scanner walks the AST, so "
            "even the TYPE_CHECKING import records the edge. The resolver cannot "
            "move to the integrations layer: runtime code (the orchestrator and "
            "audit recorder) consumes it, and runtime may not import "
            "integrations. Same wall as RepositoryThreadStateStore in "
            "docs/backend-import-migration.md."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.core.dispatch", "zeroth.runtime.orchestration.run_worker"),
        reason=(
            "zeroth.core.dispatch:RunWorker is a protected legacy capability, so "
            "the legacy dispatch package must keep republishing the run worker "
            "that now lives in the runtime layer. Resolution stays lazy: making "
            "it eager would put the orchestrator on the import path of the "
            "dispatch shim."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.core.storage.redis", "zeroth.integrations.persistence.governed_redis"),
        reason=(
            "zeroth.core.storage:GovernAIRedisRuntimeStores and "
            ":build_governai_redis_runtime are protected legacy capabilities, so "
            "the legacy storage package must keep republishing the governed store "
            "factory it no longer owns. Resolution stays lazy: making it eager "
            "would put runtime and governance code on the import path of "
            "everything that touches storage."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.core.conditions.recorder", "zeroth.runtime.runs.condition_recorder"),
        reason=(
            "zeroth.core.conditions:ConditionResultRecorder is a protected "
            "legacy capability, but the recorder mutates Run objects and so "
            "lives in the runtime run domain; the legacy conditions package "
            "must keep republishing it. Resolution stays lazy: an eager "
            "import would put the runtime run domain on the import path of "
            "the legacy conditions package."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.governance.approvals.service", "zeroth.core.runs"),
        reason=(
            "ApprovalService's pinned __init__ names RunRepository in the "
            "run_repository annotation, and the immutable legacy fixture pins "
            "that signature text, so the parameter cannot be re-annotated with "
            "a governance-owned protocol -- the same wall as "
            "RetentionErasureService. Run and RunFailureState are also "
            "consumed at runtime for resume bookkeeping. The RunStatus import "
            "moved to its contract-owned definition in zeroth.contracts."
            "governed, so this edge carries only the run bookkeeping objects."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.governance.guardrails.dead_letter", "zeroth.core.runs"),
        reason=(
            "DeadLetterManager's pinned dataclass signature names RunRepository "
            "in the run_repository annotation, and the immutable legacy fixture "
            "pins that signature text, so the field cannot be re-annotated with "
            "a governance-owned protocol -- the same wall as "
            "RetentionErasureService. RunFailureState is also constructed at "
            "runtime when a run is dead-lettered. The RunStatus import moved to "
            "its contract-owned definition in zeroth.contracts.governed, and "
            "the dead-letter reason literal is pinned locally instead of "
            "imported from the persistence module, so this edge carries only "
            "the run bookkeeping objects."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.core.agent_runtime.factory", "zeroth.service.bootstrap.factory"),
        reason=(
            "zeroth.core.agent_runtime.factory:build_runners_for_deployment is "
            "deployment-fetch wiring that moved to service bootstrap, and the "
            "legacy factory path keeps republishing it. Resolution stays lazy: "
            "an eager import would put the service domain on the import path "
            "of the legacy agent runtime package."
        ),
        removal_task="Task 18: retire the zeroth.core compatibility shell.",
    ),
    **_exception_group(
        ("zeroth.econ.analytics.adapter", "zeroth.runtime.agents.provider"),
        ("zeroth.econ.analytics.opportunities", "zeroth.governance.audit.models"),
        ("zeroth.econ.analytics.quality", "zeroth.governance.audit.models"),
        ("zeroth.econ.analytics.quality", "zeroth.core.runs.models"),
        (
            "zeroth.econ.analytics.rightsizing_experiment",
            "zeroth.runtime.agents.provider",
        ),
        (
            "zeroth.econ.analytics.rightsizing_experiment",
            "zeroth.governance.audit.models",
        ),
        ("zeroth.econ.analytics.rightsizing_experiment", "zeroth.core.eval.models"),
        ("zeroth.econ.analytics.rightsizing_experiment", "zeroth.core.eval.runner"),
        ("zeroth.econ.analytics.rightsizing_experiment", "zeroth.core.eval.scorers"),
        ("zeroth.econ.analytics.unit_economics", "zeroth.governance.audit.models"),
        ("zeroth.econ.analytics.unit_economics", "zeroth.core.runs.models"),
        ("zeroth.econ.analytics.waste", "zeroth.governance.audit.models"),
        ("zeroth.econ.analytics.waste", "zeroth.core.runs"),
        ("zeroth.econ.analytics.waste", "zeroth.core.runs.models"),
        reason=(
            "Economics analytics read runs, audit records, provider adapters, and "
            "evaluation helpers directly; the reads move behind contract-owned "
            "types when the run and audit vocabulary gains a contracts home."
        ),
        removal_task="Task 18: final migration cleanup and verification.",
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
