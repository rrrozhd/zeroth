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
        ("zeroth.contracts.graph.repository", "zeroth.runtime.graph_validation"),
        reason=(
            "GraphRepository validates on write through the public GraphValidator, "
            "which composes contract validators with execution checks and so lives "
            "in the runtime layer. Until ZER-25 the call went through the "
            "zeroth.core.graph shim, which the scanner classified as contracts, so "
            "the edge scored as contracts->contracts and never appeared."
        ),
        removal_task=(
            "Task beyond the 2026-07-18 refactor plan: give the contracts layer a "
            "validation seam the runtime registers into, so writing a graph does "
            "not reach execution checks directly. Exposed by ZER-25, which removed "
            "the shim that hid it."
        ),
    ),
    **_exception_group(
        ("zeroth.governance.approvals.service", "zeroth.integrations.persistence.runs"),
        ("zeroth.governance.approvals.service", "zeroth.runtime.runs"),
        ("zeroth.governance.guardrails.dead_letter", "zeroth.integrations.persistence.runs"),
        ("zeroth.governance.guardrails.dead_letter", "zeroth.runtime.runs"),
        ("zeroth.governance.retention.erasure_service", "zeroth.integrations.persistence.runs"),
        reason=(
            "Governance reads and writes run state: approvals resume a run, the "
            "dead-letter manager records a run failure, and retention erases a "
            "run's rows. Each names the run models and the concrete repository "
            "directly. Until ZER-25 all of it went through zeroth.core.runs, which "
            "the scanner classified as runtime, so governance->runtime scored as "
            "runtime->runtime and governance->integrations was invisible."
        ),
        removal_task=(
            "Task beyond the 2026-07-18 refactor plan: give governance a "
            "run-state port it depends on instead of the run domain and its "
            "persistence adapter. The ZER-25 spec assumed these edges would end "
            "with the legacy surface; measured after the removal, they are real "
            "dependencies the shim classification was masking, not artefacts of it."
        ),
    ),
    **_exception_group(
        ("zeroth.platform.secrets.provider", "zeroth.integrations.execution.models"),
        reason=(
            "SecretResolver's pinned legacy signatures name EnvironmentVariable in "
            "their annotations, and the dependency scanner walks the AST, so even "
            "the TYPE_CHECKING import records the edge. The resolver cannot move to "
            "the integrations layer: runtime code consumes it, and runtime may not "
            "import integrations."
        ),
        removal_task=(
            "Task beyond the 2026-07-18 refactor plan: give the platform layer a "
            "same-named structural contract for EnvironmentVariable, the way "
            "zeroth.runtime.orchestration.protocols does for RunRepository, so the "
            "pinned annotation text survives while the import does not."
        ),
    ),
    **_exception_group(
        ("zeroth.runtime.agents.thread_store", "zeroth.integrations.persistence.runs"),
        ("zeroth.runtime.orchestration.run_worker", "zeroth.integrations.persistence.runs"),
        reason=(
            "The runtime names the concrete run and thread repositories it is "
            "handed. Until ZER-25 these edges were laundered through "
            "zeroth.core.runs, which the scanner classified as runtime, so they "
            "never appeared. Converting the consumers to canonical imports "
            "exposed the real dependency rather than creating it. "
            "RuntimeOrchestrator already names its store through the "
            "runtime-owned RunRepository protocol; thread_store additionally "
            "*constructs* both repositories when none is injected, which needs a "
            "factory seam rather than an annotation change."
        ),
        removal_task=(
            "Task beyond the 2026-07-18 refactor plan: give the agent thread "
            "store and the run worker an injected "
            "repository factory so the runtime never constructs a persistence "
            "adapter, then delete these two edges. Exposed by ZER-25, which "
            "removed the legacy republisher that hid them; see "
            "zeroth.runtime.orchestration.protocols for the same-named-protocol "
            "seam the orchestrator already uses."
        ),
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
        removal_task=(
            "Task beyond the 2026-07-18 refactor plan: add a first-class "
            "provider-wrapping seam to the dispatcher's cost_estimator "
            "contract. Re-evaluated at plan close (Task 18): duck-typed "
            "estimator doubles are still relied on, so forcing the seam now "
            "would break existing callers."
        ),
    ),
    **_exception_group(
        ("zeroth.econ.analytics.adapter", "zeroth.runtime.agents.provider"),
        ("zeroth.econ.analytics.opportunities", "zeroth.governance.audit.models"),
        ("zeroth.econ.analytics.quality", "zeroth.governance.audit.models"),
        ("zeroth.econ.analytics.quality", "zeroth.runtime.runs"),
        (
            "zeroth.econ.analytics.rightsizing_experiment",
            "zeroth.runtime.agents.provider",
        ),
        (
            "zeroth.econ.analytics.rightsizing_experiment",
            "zeroth.governance.audit.models",
        ),
        ("zeroth.econ.analytics.rightsizing_experiment", "zeroth.eval.models"),
        ("zeroth.econ.analytics.rightsizing_experiment", "zeroth.eval.runner"),
        ("zeroth.econ.analytics.rightsizing_experiment", "zeroth.eval.scorers"),
        ("zeroth.econ.analytics.unit_economics", "zeroth.governance.audit.models"),
        ("zeroth.econ.analytics.unit_economics", "zeroth.runtime.runs"),
        ("zeroth.econ.analytics.waste", "zeroth.governance.audit.models"),
        ("zeroth.econ.analytics.waste", "zeroth.runtime.runs"),
        reason=(
            "Economics analytics read runs, audit records, provider adapters, and "
            "evaluation helpers directly; the reads move behind contract-owned "
            "types when the run and audit vocabulary gains a contracts home."
        ),
        removal_task=(
            "Task beyond the 2026-07-18 refactor plan: give the run and audit "
            "vocabulary a contracts home. Re-evaluated at plan close (Task 18): "
            "the pinned legacy signatures keep the model definitions at their "
            "legacy homes (canonical packages re-export), so the contracts home "
            "did not open inside the plan."
        ),
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
    """The backend domain a module belongs to, or ``None`` if it is outside them.

    ZER-25 removed the legacy-prefix table this used to consult after the
    canonical check. While it existed, a module under ``zeroth.core`` was
    classified as whichever domain now owns it, which meant a dependency routed
    through a compatibility shim was scored as if it went directly to the
    canonical package -- so several real violations were invisible. With the
    shims deleted there is one rule: the second path segment names the domain.
    """
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "zeroth" and parts[1] in BACKEND_DOMAINS:
        return parts[1]
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
