"""Minimal fail-closed execution engine for app certification declarations."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import (
    MANDATORY_CHECKS,
    AppDeclaration,
    CandidateIdentity,
    CertificationReport,
    CheckResult,
    EvidenceBinding,
    EvidenceFile,
    SmokeSpec,
)

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class CommandResult:
    """Captured result of one argv-only command."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class HttpResult:
    """Response returned by an injected HTTP boundary."""

    status_code: int
    json_body: Any


Executor = Callable[[list[str], Path], CommandResult]
HttpBoundary = Callable[[str, SmokeSpec], HttpResult]
CommitReader = Callable[[Path], str]


def execute_command(argv: list[str], cwd: Path) -> CommandResult:
    """Execute an argv array directly, never through an ambient shell."""
    completed = subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def read_git_commit(root: Path) -> str:
    """Measure the exact app commit from Git."""
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or "git returned no diagnostic"
        raise ValueError(f"cannot measure app commit: {detail}")
    return completed.stdout.strip()


def measure_candidate_identity(
    root: Path,
    declaration: AppDeclaration,
    *,
    expected_commit: str,
    image_digest: str,
    commit_reader: CommitReader = read_git_commit,
) -> CandidateIdentity:
    """Measure and validate the immutable app candidate identity."""
    if _COMMIT.fullmatch(expected_commit) is None:
        raise ValueError("expected app commit must be 40 lowercase hexadecimal characters")
    measured = commit_reader(root)
    if measured != expected_commit:
        raise ValueError(f"app commit mismatch: expected {expected_commit}, measured {measured}")
    if _DIGEST.fullmatch(image_digest) is None:
        raise ValueError("image digest must be immutable sha256:<64 lowercase hex>")
    return CandidateIdentity(
        app_name=declaration.app_name,
        app_commit=measured,
        zeroth_version=declaration.zeroth_version,
        image_reference=declaration.image_reference,
        image_digest=image_digest,
    )


def identity_digest(identity: CandidateIdentity) -> str:
    """Return the canonical digest representing a measured candidate."""
    payload = json.dumps(identity.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _subset_error(expected: Any, actual: Any, path: str = "$") -> str | None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return f"{path}: expected object, received {type(actual).__name__}"
        for key, value in expected.items():
            if key not in actual:
                return f"{path}.{key}: expected key is missing"
            error = _subset_error(value, actual[key], f"{path}.{key}")
            if error:
                return error
        return None
    if expected != actual:
        want = json.dumps(expected, sort_keys=True)
        got = json.dumps(actual, sort_keys=True)
        return f"{path}: expected {want}, received {got}"
    return None


def _output_tail(result: CommandResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "no command output"
    return detail[-500:]


class CertificationRunner:
    """Execute all mandatory checks and always return a complete report."""

    def __init__(
        self,
        root: Path,
        declaration: AppDeclaration,
        *,
        executor: Executor = execute_command,
        http: HttpBoundary | None = None,
        commit_reader: CommitReader = read_git_commit,
    ) -> None:
        self.root = root.resolve()
        self.declaration = declaration
        self.executor = executor
        self.http = http
        self.commit_reader = commit_reader

    def run(self, *, expected_commit: str, image_digest: str) -> CertificationReport:
        identity, identity_error = self._identity(expected_commit, image_digest)
        checks: list[CheckResult] = []
        evidence: dict[str, EvidenceFile] = {}
        for name in MANDATORY_CHECKS:
            result, record = self._run_check(name, identity_error)
            checks.append(result)
            if record is not None:
                evidence[name] = record
        binding = self._binding(identity, checks, evidence)
        status = "passed" if all(item.status == "passed" for item in checks) else "failed"
        return CertificationReport(
            status=status,
            candidate=identity,
            checks=checks,
            evidence=binding,
        )

    def _identity(
        self, expected_commit: str, image_digest: str
    ) -> tuple[CandidateIdentity | None, str | None]:
        try:
            identity = measure_candidate_identity(
                self.root,
                self.declaration,
                expected_commit=expected_commit,
                image_digest=image_digest,
                commit_reader=self.commit_reader,
            )
        except Exception as error:  # noqa: BLE001 - identity faults become report evidence
            return None, str(error)
        return identity, None

    def _run_check(
        self, name: str, identity_error: str | None
    ) -> tuple[CheckResult, EvidenceFile | None]:
        if name == "dependency-lock":
            error = self._file_error(self.declaration.lock_path, "dependency lock")
            if error:
                return self._failed(name, error), None
        command = self._command(name)
        if command.status == "failed":
            return command, None
        if name in {"packaged-smoke", "ephemeral-smoke"}:
            return self._smoke(name), None
        if name in {"sbom", "provenance"}:
            if identity_error:
                return self._failed(name, f"candidate identity invalid: {identity_error}"), None
            path = getattr(self.declaration, f"{name}_path")
            return self._evidence(name, path)
        return command, None

    def _command(self, name: str) -> CheckResult:
        argv = list(self.declaration.checks[name])
        try:
            result = self.executor(argv, self.root)
        except Exception as error:  # noqa: BLE001 - command faults are named check failures
            return self._failed(name, f"argv execution raised {type(error).__name__}: {error}")
        if not isinstance(result, CommandResult):
            return self._failed(name, "argv executor returned no CommandResult")
        if result.returncode:
            return self._failed(name, f"argv exited {result.returncode}: {_output_tail(result)}")
        return CheckResult(name=name, status="passed", detail=f"{name} argv completed")

    def _smoke(self, name: str) -> CheckResult:
        if self.http is None:
            return self._failed(name, "HTTP boundary is not configured")
        try:
            response = self.http(name, self.declaration.smoke)
        except Exception as error:  # noqa: BLE001 - transport faults become retained diagnostics
            return self._failed(name, f"HTTP request raised {type(error).__name__}: {error}")
        try:
            expected = self.declaration.smoke.expected_status
            if response.status_code != expected:
                return self._failed(
                    name, f"expected HTTP {expected}, received {response.status_code}"
                )
            error = _subset_error(self.declaration.smoke.expected_json, response.json_body)
        except Exception as failure:  # noqa: BLE001 - malformed results must remain reportable
            return self._failed(
                name, f"malformed HTTP result: {type(failure).__name__}: {failure}"
            )
        if error:
            return self._failed(name, f"JSON subset mismatch: {error}")
        return CheckResult(name=name, status="passed", detail=f"{name} HTTP assertions passed")

    def _resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"path {relative!r} resolves outside the app root") from error
        return path

    def _file_error(self, relative: str, label: str) -> str | None:
        try:
            path = self._resolve(relative)
            if not path.is_file():
                return f"{label} {relative!r} is missing"
            if path.stat().st_size == 0:
                return f"{label} {relative!r} is empty"
        except OSError as error:
            return f"{label} {relative!r} is unreadable: {error}"
        except ValueError as error:
            return str(error)
        return None

    def _evidence(self, name: str, relative: str) -> tuple[CheckResult, EvidenceFile | None]:
        error = self._file_error(relative, name)
        if error:
            return self._failed(name, error), None
        try:
            payload = self._resolve(relative).read_bytes()
        except OSError as error:
            return self._failed(name, f"{name} {relative!r} is unreadable: {error}"), None
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        record = EvidenceFile(path=relative, sha256=digest)
        return CheckResult(name=name, status="passed", detail=f"{name} evidence retained"), record

    @staticmethod
    def _failed(name: str, detail: str) -> CheckResult:
        return CheckResult(name=name, status="failed", detail=f"{name}: {detail}")

    @staticmethod
    def _binding(
        identity: CandidateIdentity | None,
        checks: list[CheckResult],
        evidence: dict[str, EvidenceFile],
    ) -> EvidenceBinding | None:
        by_name = {item.name: item for item in checks}
        names = ("sbom", "provenance")
        if identity is None or any(by_name[name].status != "passed" for name in names):
            return None
        return EvidenceBinding(
            candidate_identity_digest=identity_digest(identity),
            sbom=evidence["sbom"],
            provenance=evidence["provenance"],
        )
