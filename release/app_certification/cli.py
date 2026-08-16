"""Command-line entry point for app declaration and report validation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from .evidence import (
    bind_sbom,
    finalize_attestation,
    validate_image_archive,
    validate_source_archive,
    write_provenance,
)
from .http_process import run_http_exchange
from .models import (
    MANDATORY_CHECKS,
    SmokeSpec,
    file_digest,
    identity_digest,
    load_declaration,
    validate_report,
    write_report,
)
from .runner import (
    CertificationRunner,
    Executor,
    HttpResult,
    execute_command,
    measure_candidate_identity,
)
from .scaffold import scaffold_checkout

_HTTP_TIMEOUT_SECONDS = 30.0
_READINESS_TIMEOUT_SECONDS = 5.0


class UrlHttpBoundary:
    """Execute declared smoke requests against two caller-supplied origins."""

    def __init__(
        self,
        packaged_url: str,
        ephemeral_url: str,
        headers: Mapping[str, str],
    ) -> None:
        self.urls = {
            "packaged-smoke": packaged_url.rstrip("/"),
            "ephemeral-smoke": ephemeral_url.rstrip("/"),
        }
        self.headers = {"Content-Type": "application/json", **headers}

    def __call__(self, check: str, smoke: SmokeSpec) -> HttpResult:
        status, body = run_http_exchange(
            self.urls[check] + smoke.path,
            method=smoke.method,
            headers=self.headers,
            body=smoke.request_json,
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        return HttpResult(status, body)


def _untrusted_executor(user: str) -> Executor:
    if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) is None:
        raise ValueError("untrusted user must be a simple local account name")
    clean_environment = (
        "env",
        "-i",
        f"HOME=/home/{user}",
        "LANG=C.UTF-8",
        f"PATH={os.environ.get('PATH', '')}",
    )
    prefix = ("sudo", "--non-interactive", "--user", user, "--", *clean_environment)

    def run(argv: list[str], cwd: Path):
        return execute_command([*prefix, *argv], cwd)

    return run


def resolve_smoke_headers(
    smoke: SmokeSpec,
    environ: Mapping[str, str] = os.environ,
) -> dict[str, str]:
    """Resolve validated header names without persisting secret values."""
    headers: dict[str, str] = {}
    for header, env_name in smoke.headers_from_env.items():
        value = environ.get(env_name)
        if not value:
            raise ValueError(f"smoke HTTP header {header!r} requires environment {env_name}")
        headers[header] = value
    return headers


def _add_handoff_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--report", type=Path, required=True)
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--image-archive", type=Path, required=True)
    command.add_argument("--source-archive", type=Path, required=True)
    command.add_argument("--app-commit", required=True)
    command.add_argument("--zeroth-version", required=True)
    command.add_argument("--zeroth-commit", required=True)
    command.add_argument("--verdict", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app-certification")
    commands = parser.add_subparsers(dest="command", required=True)
    declaration = commands.add_parser("validate-declaration")
    declaration.add_argument("--declaration", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--declaration", type=Path, required=True)
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--app-commit", required=True)
    run.add_argument("--image-digest", required=True)
    run.add_argument("--source-digest", required=True)
    run.add_argument("--packaged-url", required=True)
    run.add_argument("--ephemeral-url", required=True)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--check-python", type=Path)
    run.add_argument("--untrusted-user")
    report = commands.add_parser("validate-report")
    report.add_argument("--report", type=Path, required=True)
    report.add_argument("--root", type=Path)
    evidence = commands.add_parser("prepare-evidence")
    evidence.add_argument("--declaration", type=Path, required=True)
    evidence.add_argument("--root", type=Path, required=True)
    evidence.add_argument("--app-commit", required=True)
    evidence.add_argument("--image-digest", required=True)
    evidence.add_argument("--source-digest", required=True)
    evidence.add_argument("--raw-sbom", type=Path, required=True)
    handoff = commands.add_parser("validate-handoff")
    _add_handoff_arguments(handoff)
    attestation = commands.add_parser("finalize-attestation")
    attestation.add_argument("--bundle", type=Path, required=True)
    _add_handoff_arguments(attestation)
    probe = commands.add_parser("probe-readiness")
    probe.add_argument("--url", required=True)
    finalizer = commands.add_parser("finalize-workflow")
    finalizer.add_argument("--root", type=Path, required=True)
    scaffold = commands.add_parser("scaffold")
    scaffold.add_argument("--root", type=Path, required=True)
    scaffold.add_argument("--app-name", required=True)
    scaffold.add_argument("--module", required=True)
    scaffold.add_argument("--zeroth-version", required=True)
    scaffold.add_argument("--zeroth-ref", required=True)
    return parser


def _run(args: argparse.Namespace) -> int:
    declaration = load_declaration(args.declaration)
    headers = resolve_smoke_headers(declaration.smoke)
    http = UrlHttpBoundary(args.packaged_url, args.ephemeral_url, headers)
    report = CertificationRunner(
        args.root,
        declaration,
        executor=execute_command,
        candidate_executor=_untrusted_executor(args.untrusted_user)
        if args.untrusted_user
        else execute_command,
        http=http,
        declaration_path=args.declaration,
        check_python=args.check_python,
    ).run(
        expected_commit=args.app_commit,
        image_digest=args.image_digest,
        source_digest=args.source_digest,
    )
    write_report(report, args.report)
    print(f"app certification: {report.status}; report={args.report}")
    return 0 if report.status == "passed" else 1


def _destination(root: Path, relative: str) -> Path:
    root = root.resolve()
    destination = (root / relative).resolve()
    destination.relative_to(root)
    return destination


def _prepare_evidence(args: argparse.Namespace) -> int:
    declaration = load_declaration(args.declaration)
    candidate = measure_candidate_identity(
        args.root,
        declaration,
        expected_commit=args.app_commit,
        image_digest=args.image_digest,
        source_digest=args.source_digest,
    )
    bind_sbom(args.raw_sbom, candidate)
    certification_root = args.root / ".app-certification"
    certification_root.mkdir(parents=True, exist_ok=True)
    sbom = _destination(args.root, declaration.sbom_path)
    provenance = _destination(args.root, declaration.provenance_path)
    sbom.parent.mkdir(parents=True, exist_ok=True)
    if sbom.resolve() != args.raw_sbom.resolve():
        shutil.copyfile(args.raw_sbom, sbom)
    write_provenance(provenance, candidate)
    retained = certification_root / "root"
    for source, relative in (
        (sbom, declaration.sbom_path),
        (provenance, declaration.provenance_path),
    ):
        destination = _destination(retained, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    (certification_root / "candidate.json").write_text(
        json.dumps(candidate.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


def _validate_handoff(args: argparse.Namespace) -> int:
    report = validate_report(args.report, root=args.root)
    candidate = report.candidate
    if report.status != "passed" or candidate is None or report.evidence is None:
        raise ValueError("handoff requires a passing candidate-bound report")
    if candidate.app_commit != args.app_commit:
        raise ValueError("handoff app commit does not match the workflow candidate")
    if candidate.zeroth_version != args.zeroth_version:
        raise ValueError("handoff Zeroth version does not match the trusted certifier")
    if re.fullmatch(r"[0-9a-f]{40}", args.zeroth_commit) is None:
        raise ValueError("trusted certifier commit must be a full commit SHA")
    validate_image_archive(args.image_archive, candidate)
    validate_source_archive(args.source_archive, candidate)
    verdict = {
        "schema_version": 1,
        "app_commit": candidate.app_commit,
        "zeroth_commit": args.zeroth_commit,
        "zeroth_version": candidate.zeroth_version,
        "image_reference": candidate.image_reference,
        "image_digest": candidate.image_digest,
        "source_digest": candidate.source_digest,
        "candidate_identity_digest": identity_digest(candidate),
        "report_sha256": file_digest(args.report),
        "image_archive_sha256": file_digest(args.image_archive),
        "provenance_path": report.evidence.provenance.path,
    }
    args.verdict.write_text(
        json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"image_reference={candidate.image_reference}")
    print(f"image_digest={candidate.image_digest}")
    print(f"provenance_path={report.evidence.provenance.path}")
    print(f"verdict_sha256={file_digest(args.verdict)}")
    return 0


def _finalize_workflow(root: Path) -> int:
    stages = {
        name.lower(): os.environ.get(name, "skipped")
        for name in (
            "APP_CHECKOUT",
            "CERTIFIER_CHECKOUT",
            "PREPARE",
            "IMAGE",
            "SBOM",
            "EVIDENCE",
            "CONTAINERS",
            "HEALTH",
            "CERTIFY",
        )
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "workflow-stages.json").write_text(
        json.dumps(stages, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if (root / "report.json").exists():
        return 0
    stage = {
        "container-startup": "containers",
        "health": "health",
        "packaged-smoke": "certify",
        "ephemeral-smoke": "certify",
        "sbom": "sbom",
        "provenance": "evidence",
    }
    checks = [
        {
            "name": name,
            "status": "failed",
            "detail": f"{name}: workflow stage {stage.get(name, 'prepare')} "
            f"outcome={stages[stage.get(name, 'prepare')]}",
        }
        for name in MANDATORY_CHECKS
    ]
    payload = {"schema_version": 1, "status": "failed", "candidate": None, "checks": checks}
    payload["evidence"] = None
    (root / "report.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


def _probe_readiness(url: str) -> int:
    status, payload = run_http_exchange(
        url,
        method="GET",
        headers={"Accept": "application/json"},
        body=None,
        timeout=_READINESS_TIMEOUT_SECONDS,
    )
    if status != 200:
        raise ValueError(f"readiness expected HTTP 200, received {status}")
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValueError(f"readiness status must be 'ok', received {payload!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Validate declarations, execute certification, or validate retained reports."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate-declaration":
            load_declaration(args.declaration)
            print(f"valid declaration: {args.declaration}")
            return 0
        if args.command == "run":
            return _run(args)
        if args.command == "validate-report":
            report = validate_report(args.report, root=args.root)
            print(f"valid report: {args.report}; status={report.status}")
            return 0 if report.status == "passed" else 1
        if args.command == "prepare-evidence":
            return _prepare_evidence(args)
        if args.command == "validate-handoff":
            return _validate_handoff(args)
        if args.command == "finalize-attestation":
            finalize_attestation(args.bundle, args.report, args.root)
            return _validate_handoff(args)
        if args.command == "probe-readiness":
            return _probe_readiness(args.url)
        if args.command == "finalize-workflow":
            return _finalize_workflow(args.root)
        scaffold_checkout(
            args.root,
            app_name=args.app_name,
            module=args.module,
            zeroth_version=args.zeroth_version,
            zeroth_ref=args.zeroth_ref,
        )
        return 0
    except (OSError, ValueError) as error:
        print(f"app certification error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
