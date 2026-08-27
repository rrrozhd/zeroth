"""Serial command runner with per-gate working-directory isolation."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .evidence import EvidenceStore

_SAFE_COMMAND_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class CommandSpec:
    """One fail-closed evaluation gate."""

    name: str
    argv: tuple[str, ...]
    isolated: bool = True
    cwd: Path | None = None


@dataclass(frozen=True)
class CommandResult:
    """Captured result for one attempted gate."""

    name: str
    exit_code: int
    working_directory: Path
    stdout: str
    stderr: str
    evidence_path: Path | None = None


@dataclass(frozen=True)
class EvaluationReport:
    """Ordered results; a failed gate prevents every later gate from running."""

    results: tuple[CommandResult, ...]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(result.exit_code == 0 for result in self.results)


def baseline_commands(repository_root: Path) -> tuple[CommandSpec, ...]:
    """Return the no-provider baseline gates in their required serial order."""
    root = repository_root.resolve()

    def pytest_gate(name: str, *paths: str) -> CommandSpec:
        return CommandSpec(
            name=name,
            argv=(
                "uv",
                "run",
                "--project",
                str(root),
                "pytest",
                *(str(root / path) for path in paths),
                "-q",
            ),
            isolated=True,
        )

    frontend = root / "frontend"
    return (
        pytest_gate(
            "studio-contracts",
            "tests/service/test_studio_workspace_isolation.py",
            "tests/test_studio_api.py",
        ),
        pytest_gate(
            "budget-contracts",
            "tests/orchestrator/test_per_run_cap.py",
            "tests/orchestrator/test_memory_budget_wiring.py",
            "tests/econ/test_instrumentation_invariants.py",
        ),
        pytest_gate(
            "evidence-contracts",
            "tests/service/test_evidence_api.py",
            "tests/service/test_provenance_signing_api.py",
            "tests/test_econ_adapter.py",
        ),
        CommandSpec(
            name="frontend-behavior",
            argv=("npm", "test", "--", "--run"),
            isolated=False,
            cwd=frontend,
        ),
        CommandSpec(
            name="frontend-production-build",
            argv=("npm", "run", "build"),
            isolated=False,
            cwd=frontend,
        ),
    )


def execute_commands(
    commands: list[CommandSpec] | tuple[CommandSpec, ...],
    *,
    artifact_root: Path,
    evidence_store: EvidenceStore | None = None,
) -> EvaluationReport:
    """Execute gates serially and stop at the first non-zero exit status."""
    if evidence_store is None:
        raise ValueError("campaign command execution requires an evidence_store")
    for command in commands:
        if not _SAFE_COMMAND_NAME.fullmatch(command.name):
            raise ValueError("command evidence name must be a safe slug")
    work_root = artifact_root / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    results: list[CommandResult] = []
    for command in commands:
        if command.isolated:
            working_directory = Path(
                tempfile.mkdtemp(prefix=f"{command.name}-", dir=work_root)
            )
        elif command.cwd is not None:
            working_directory = command.cwd
        else:
            raise ValueError(f"non-isolated command {command.name!r} requires cwd")
        completed = subprocess.run(
            command.argv,
            cwd=working_directory,
            check=False,
            capture_output=True,
            text=True,
        )
        evidence_path = None
        recorded = evidence_store.record_command(
            sequence=len(results) + 1,
            name=command.name,
            argv=command.argv,
            working_directory=working_directory,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        evidence_path = recorded.relative_to(evidence_store.root)
        results.append(
            CommandResult(
                name=command.name,
                exit_code=completed.returncode,
                working_directory=working_directory,
                stdout=completed.stdout,
                stderr=completed.stderr,
                evidence_path=evidence_path,
            )
        )
        if completed.returncode != 0:
            break
    return EvaluationReport(results=tuple(results))
