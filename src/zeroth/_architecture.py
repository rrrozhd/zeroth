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
        ("zeroth.core.agent_runtime.thread_store", "zeroth.core.runs.repository"),
        ("zeroth.core.runs", "zeroth.core.runs.repository"),
        reason="Runtime code still reaches the concrete legacy run repository.",
        removal_task="Task 6: split concrete run persistence and inject runtime protocols.",
    ),
    **_exception_group(
        ("zeroth.core.graph.validation", "zeroth.core.parallel.errors"),
        ("zeroth.core.graph.validation", "zeroth.core.parallel.reducers"),
        ("zeroth.core.graph.validation", "zeroth.core.policy.models"),
        ("zeroth.core.graph.validation", "zeroth.core.execution_units.inline"),
        reason="The legacy graph validator mixes contract and execution validation.",
        removal_task="Task 7: decompose graph validation behind contract-owned validators.",
    ),
    **_exception_group(
        ("zeroth.core.orchestrator.runtime", "zeroth.core.execution_units"),
        (
            "zeroth.core.orchestrator.runtime",
            "zeroth.core.governed.memory.models",
        ),
        ("zeroth.core.orchestrator.runtime", "zeroth.core.execution_units.inline"),
        ("zeroth.core.orchestrator.runtime", "zeroth.core.econ.adapter"),
        reason="The monolithic orchestrator directly constructs integration collaborators.",
        removal_task="Task 8: decompose orchestration and inject integration collaborators.",
    ),
    **_exception_group(
        ("zeroth.core.retention.econ_eraser", "zeroth.econ_plane.database"),
        (
            "zeroth.core.retention.econ_eraser",
            "zeroth.econ_plane.instrumentation.models",
        ),
        ("zeroth.core.retention.erasure_service", "zeroth.core.runs.repository"),
        reason="Retention orchestration directly owns concrete persistence cleanup.",
        removal_task="Task 9: decompose retention erasure behind injected cleanup adapters.",
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
        ("zeroth.core.guardrails.dead_letter", "zeroth.core.runs.repository"),
        ("zeroth.core.policy.guard", "zeroth.core.runs"),
        reason="Legacy governance services depend directly on runtime implementations.",
        removal_task="Task 13: move governance packages behind contract-owned interfaces.",
    ),
    **_exception_group(
        ("zeroth.core.agent_runtime.factory", "zeroth.core.deployments"),
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


def _imports(path: Path, source_root: Path) -> list[tuple[int, str]]:
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
            if node.module is None:
                imported.extend(
                    (node.lineno, f"{base}.{alias.name}".strip("."))
                    for alias in node.names
                    if alias.name != "*"
                )
            elif base:
                imported.append((node.lineno, base))
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
    violations: list[DependencyViolation] = []
    for path in scanned_files:
        importer, _ = _module_name(path, source_root)
        imports = _imports(path, source_root)
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
