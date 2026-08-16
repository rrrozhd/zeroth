"""Minimal fail-closed execution engine for app certification declarations."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import validate_evidence_subject
from .models import (
    MANDATORY_CHECKS,
    AppDeclaration,
    CandidateIdentity,
    CertificationReport,
    CheckResult,
    EvidenceBinding,
    EvidenceFile,
    SmokeSpec,
    file_digest,
    identity_digest,
)

_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECK_BOOTSTRAP = (
    "import pathlib,runpy,sys;"
    "certifier=pathlib.Path(sys.argv.pop(1));"
    "venv=pathlib.Path(sys.argv.pop(1));"
    "site_packages=venv/'lib'/f'python{sys.version_info.major}.{sys.version_info.minor}'/"
    "'site-packages';"
    "sys.prefix=sys.exec_prefix=str(venv);"
    "sys.path[:0]=[str(certifier),str(certifier/'src'),str(site_packages)];"
    "runpy.run_module('release.app_certification.checks',run_name='__main__')"
)
_CANDIDATE_BOOTSTRAP = _CHECK_BOOTSTRAP.replace(
    "release.app_certification.checks", "release.app_certification.candidate_worker"
)


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
    """Execute one certifier-owned argv with a bounded process lifetime."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        detail = stderr or "certifier-owned check timed out after 180s"
        return CommandResult(124, stdout, detail)
    return CommandResult(process.returncode, stdout, stderr)


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
    source_digest: str,
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
    if _DIGEST.fullmatch(source_digest) is None:
        raise ValueError("source digest must be immutable sha256:<64 lowercase hex>")
    return CandidateIdentity(
        app_name=declaration.app_name,
        app_commit=measured,
        zeroth_version=declaration.zeroth_version,
        image_reference=declaration.image_reference,
        image_digest=image_digest,
        source_digest=source_digest,
    )


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
        candidate_executor: Executor | None = None,
        http: HttpBoundary | None = None,
        commit_reader: CommitReader = read_git_commit,
        declaration_path: Path | None = None,
        check_python: Path | None = None,
    ) -> None:
        self.root = root.resolve()
        self.declaration = declaration
        self.executor = executor
        self.candidate_executor = candidate_executor or executor
        self.http = http
        self.commit_reader = commit_reader
        self.declaration_path = (declaration_path or self.root / "certification.json").resolve()
        self.check_python = (check_python or Path(sys.executable)).absolute()

    def run(
        self, *, expected_commit: str, image_digest: str, source_digest: str
    ) -> CertificationReport:
        identity, identity_error = self._identity(expected_commit, image_digest, source_digest)
        checks: list[CheckResult] = []
        evidence: dict[str, EvidenceFile] = {}
        for name in MANDATORY_CHECKS:
            result, record = self._run_check(name, identity, identity_error)
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
        self, expected_commit: str, image_digest: str, source_digest: str
    ) -> tuple[CandidateIdentity | None, str | None]:
        try:
            identity = measure_candidate_identity(
                self.root,
                self.declaration,
                expected_commit=expected_commit,
                image_digest=image_digest,
                source_digest=source_digest,
                commit_reader=self.commit_reader,
            )
        except Exception as error:  # noqa: BLE001 - identity faults become report evidence
            return None, str(error)
        return identity, None

    def _run_check(
        self,
        name: str,
        identity: CandidateIdentity | None,
        identity_error: str | None,
    ) -> tuple[CheckResult, EvidenceFile | None]:
        if name in {"packaged-smoke", "ephemeral-smoke"}:
            return self._smoke(name), None
        if name in {"sbom", "provenance"}:
            if identity_error:
                return self._failed(name, f"candidate identity invalid: {identity_error}"), None
            path = getattr(self.declaration, f"{name}_path")
            evidence = self._evidence(name, path, identity)
            if isinstance(evidence, CheckResult):
                return evidence, None
            result = CheckResult(name=name, status="passed", detail=f"{name} evidence retained")
            return result, evidence
        return self._command(name), None

    def _command(self, name: str) -> CheckResult:
        from .candidate_worker import CANDIDATE_CHECKS

        if name in CANDIDATE_CHECKS:
            return self._candidate_command(name)
        return self._trusted_command(name)

    def _command_result(
        self, name: str, argv: list[str], executor: Executor
    ) -> CommandResult | CheckResult:
        try:
            result = executor(argv, self.root)
        except Exception as error:  # noqa: BLE001 - command faults are named check failures
            return self._failed(name, f"argv execution raised {type(error).__name__}: {error}")
        if not isinstance(result, CommandResult):
            return self._failed(name, "argv executor returned no CommandResult")
        if result.returncode:
            return self._failed(name, f"argv exited {result.returncode}: {_output_tail(result)}")
        return result

    def _trusted_command(self, name: str) -> CheckResult:
        argv = [
            str(Path(sys.executable).absolute()),
            "-I",
            "-S",
            "-c",
            _CHECK_BOOTSTRAP,
            str(Path(__file__).parents[2].resolve()),
            str(self.check_python.parent.parent),
            name,
            "--root",
            str(self.root),
            "--declaration",
            str(self.declaration_path),
        ]
        result = self._command_result(name, argv, self.executor)
        if isinstance(result, CheckResult):
            return result
        try:
            structured = json.loads(result.stdout.splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return self._failed(name, "owned structured result is missing or malformed")
        expected = {"check": name, "schema_version": 1, "status": "passed"}
        if structured != expected:
            return self._failed(name, "owned structured result does not match the requested check")
        return CheckResult(name=name, status="passed", detail=f"{name} semantic check completed")

    def _candidate_command(self, name: str) -> CheckResult:
        from .candidate_worker import finalize_candidate_evidence

        argv = [
            str(Path(sys.executable).absolute()),
            "-I",
            "-S",
            "-c",
            _CANDIDATE_BOOTSTRAP,
            str(Path(__file__).parents[2].resolve()),
            str(self.check_python.parent.parent),
            name,
            "--root",
            str(self.root),
            "--declaration-json",
            self.declaration.model_dump_json(),
        ]
        result = self._command_result(name, argv, self.candidate_executor)
        if isinstance(result, CheckResult):
            return result
        try:
            payload = json.loads(result.stdout.splitlines()[-1])
            finalize_candidate_evidence(name, payload, self.declaration)
        except Exception as error:  # noqa: BLE001 - untrusted evidence must fail closed
            return self._failed(name, f"trusted finalization rejected candidate evidence: {error}")
        return CheckResult(name=name, status="passed", detail=f"{name} semantic check finalized")

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
            return self._failed(name, f"malformed HTTP result: {type(failure).__name__}: {failure}")
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

    def _evidence(
        self,
        name: str,
        relative: str,
        identity: CandidateIdentity | None,
    ) -> EvidenceFile | CheckResult:
        error = self._file_error(relative, name)
        if error:
            return self._failed(name, error)
        try:
            path = self._resolve(relative)
            if identity is None:
                raise ValueError("candidate identity is unavailable")
            validate_evidence_subject(name, path, identity)
            return EvidenceFile(path=relative, sha256=file_digest(path))
        except (OSError, ValueError) as error:
            return self._failed(name, f"{name} evidence is invalid: {error}")

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
