"""Command-line entry point for app declaration and report validation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from .evidence import (
    bind_sbom,
    finalize_attestation,
    validate_evidence_subject,
    validate_image_archive,
    validate_source_archive,
    write_provenance,
)
from .http_process import probe_readiness as _probe_readiness
from .http_process import run_http_exchange
from .models import (
    CandidateIdentity,
    CertificationReport,
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
from .runtime_probe import probe_regulus
from .runtime_probe_worker import probe_runtime_extras
from .scaffold import generate_semantic_manifest, scaffold_checkout
from .wheel_installation import (
    build_material_digests,
    prepare_runtime_context,
    verify_wheel_installation,
)
from .workflow_finalizer import (
    finalize_workflow as _finalize_workflow,
)
from .workflow_finalizer import (
    validate_workflow_evidence,
    write_workflow_evidence,
)

_HTTP_TIMEOUT_SECONDS = 30.0


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
    database_environment = tuple(
        f"{name}={os.environ[name]}"
        for name in ("ZEROTH_DATABASE__BACKEND", "ZEROTH_DATABASE__POSTGRES_DSN")
        if name in os.environ
    )
    clean_environment = (
        "env",
        "-i",
        f"HOME=/home/{user}",
        "LANG=C.UTF-8",
        f"PATH={os.environ.get('PATH', '')}",
        *database_environment,
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
    command.add_argument("--workflow-evidence", type=Path, required=True)
    command.add_argument("--workflow-stages", type=Path, required=True)
    command.add_argument("--cleanup", type=Path, required=True)
    command.add_argument("--image-archive", type=Path, required=True)
    command.add_argument("--source-archive", type=Path, required=True)
    command.add_argument("--app-repository", type=Path, required=True)
    command.add_argument("--app-commit", required=True)
    command.add_argument("--zeroth-version", required=True)
    command.add_argument("--zeroth-commit", required=True)
    command.add_argument("--certifier-wheel", type=Path, required=True)
    command.add_argument("--requirements-lock", type=Path, required=True)
    command.add_argument("--wheel-installation", type=Path, required=True)
    command.add_argument("--image-config", type=Path, required=True)
    command.add_argument("--verdict", type=Path, required=True)


def _add_wheel_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--wheel", type=Path, required=True)
    command.add_argument("--site-packages", type=Path, required=True)
    command.add_argument("--image-config", type=Path, required=True)
    command.add_argument("--image-digest", required=True)
    command.add_argument("--output", type=Path, required=True)


def _add_runtime_context_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--source-root", type=Path, required=True)
    command.add_argument("--certifier-wheel", type=Path, required=True)
    command.add_argument("--requirements-lock", type=Path, required=True)
    command.add_argument("--image-config", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)


def _add_scaffold_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--app-name", required=True)
    command.add_argument("--module", required=True)
    command.add_argument("--zeroth-version", required=True)
    command.add_argument("--zeroth-ref", required=True)


def _add_probe_commands(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("probe-readiness").add_argument("--url", required=True)
    runtime = commands.add_parser("probe-runtime-extras")
    runtime.add_argument("--zeroth-version", required=True)
    regulus = commands.add_parser("probe-regulus")
    regulus.add_argument("--url", required=True)
    regulus.add_argument("--tenant", required=True)
    regulus.add_argument("--probe-id", required=True)
    regulus.add_argument("--mode", choices=("packaged", "ephemeral"), required=True)


def _add_semantic_commands(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("finalize-workflow").add_argument("--root", type=Path, required=True)
    semantic = commands.add_parser("generate-semantic")
    semantic.add_argument("--root", type=Path, required=True)
    semantic.add_argument("--declaration", type=Path, required=True)
    semantic.add_argument("--output", type=Path, required=True)
    semantic.add_argument("--database-backend", choices=("sqlite", "postgres"), default="sqlite")
    _add_scaffold_arguments(commands.add_parser("scaffold"))


def _add_attestation_command(commands: argparse._SubParsersAction) -> None:
    attestation = commands.add_parser("finalize-attestation")
    attestation.add_argument("--bundle", type=Path, required=True)
    attestation.add_argument("--repository", required=True)
    attestation.add_argument("--signer-repo", required=True)
    attestation.add_argument("--signer-workflow", required=True)
    attestation.add_argument("--signer-digest", required=True)
    _add_handoff_arguments(attestation)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app-certification")
    commands = parser.add_subparsers(dest="command", required=True)
    declaration = commands.add_parser("validate-declaration")
    declaration.add_argument("--declaration", type=Path, required=True)
    declaration.add_argument("--root", type=Path)
    run = commands.add_parser("run")
    run.add_argument("--declaration", type=Path, required=True)
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--app-commit", required=True)
    run.add_argument("--image-digest", required=True)
    run.add_argument("--image-reference")
    run.add_argument("--source-digest", required=True)
    run.add_argument("--packaged-url", required=True)
    run.add_argument("--ephemeral-url", required=True)
    run.add_argument("--report", type=Path, required=True)
    run.add_argument("--check-python", type=Path)
    run.add_argument("--untrusted-user")
    run.add_argument("--evidence-root", type=Path)
    report = commands.add_parser("validate-report")
    report.add_argument("--report", type=Path, required=True)
    report.add_argument("--root", type=Path)
    evidence = commands.add_parser("prepare-evidence")
    evidence.add_argument("--declaration", type=Path, required=True)
    evidence.add_argument("--root", type=Path, required=True)
    evidence.add_argument("--app-commit", required=True)
    evidence.add_argument("--image-digest", required=True)
    evidence.add_argument("--image-reference")
    evidence.add_argument("--source-digest", required=True)
    evidence.add_argument("--raw-sbom", type=Path, required=True)
    evidence.add_argument("--zeroth-commit", required=True)
    evidence.add_argument("--certifier-wheel", type=Path, required=True)
    evidence.add_argument("--requirements-lock", type=Path, required=True)
    evidence.add_argument("--wheel-installation", type=Path, required=True)
    evidence.add_argument("--image-config", type=Path, required=True)
    evidence.add_argument("--output-root", type=Path, required=True)
    wheel = commands.add_parser("verify-wheel-installation")
    _add_wheel_arguments(wheel)
    _add_runtime_context_arguments(commands.add_parser("prepare-runtime-context"))
    handoff = commands.add_parser("validate-handoff")
    _add_handoff_arguments(handoff)
    _add_attestation_command(commands)
    _add_probe_commands(commands)
    _add_semantic_commands(commands)
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
        evidence_root=args.evidence_root,
        untrusted_user=args.untrusted_user,
    ).run(
        expected_commit=args.app_commit,
        image_reference=args.image_reference,
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
        image_reference=args.image_reference,
        image_digest=args.image_digest,
        source_digest=args.source_digest,
    )
    bind_sbom(args.raw_sbom, candidate)
    if args.output_root.is_symlink():
        raise ValueError("certification output root must not be a symlink")
    certification_root = args.output_root.resolve()
    certification_root.mkdir(parents=True, exist_ok=True)
    retained = certification_root / "root"
    sbom = _destination(retained, declaration.sbom_path)
    provenance = _destination(retained, declaration.provenance_path)
    sbom.parent.mkdir(parents=True, exist_ok=True)
    if sbom.resolve() != args.raw_sbom.resolve():
        shutil.copyfile(args.raw_sbom, sbom)
    if re.fullmatch(r"[0-9a-f]{40}", args.zeroth_commit) is None:
        raise ValueError("trusted certifier commit must be a full commit SHA")
    materials = build_material_digests(
        candidate,
        sbom,
        args.certifier_wheel,
        args.requirements_lock,
        args.wheel_installation,
        args.image_config,
    )
    write_provenance(
        provenance,
        candidate,
        zeroth_commit=args.zeroth_commit,
        sbom_digest=file_digest(sbom),
        build_material_digests=materials,
    )
    _retain_build_materials(certification_root, args)
    (certification_root / "candidate.json").write_text(
        json.dumps(candidate.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


def _retain_build_materials(root: Path, args: argparse.Namespace) -> None:
    materials = root / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.certifier_wheel, materials / "zeroth-core.whl")
    shutil.copyfile(args.requirements_lock, materials / "requirements-image.txt")
    shutil.copyfile(args.wheel_installation, materials / "installed-wheel.json")
    shutil.copyfile(args.image_config, materials / "image-config.json")


def _handoff_material_digests(
    args: argparse.Namespace, candidate: CandidateIdentity, sbom: Path
) -> dict[str, str]:
    return build_material_digests(
        candidate,
        sbom,
        args.certifier_wheel,
        args.requirements_lock,
        args.wheel_installation,
        args.image_config,
    )


def _validated_handoff_candidate(
    args: argparse.Namespace, report: CertificationReport
) -> CandidateIdentity:
    candidate = report.candidate
    if report.status != "passed" or candidate is None or report.evidence is None:
        raise ValueError("handoff requires a passing candidate-bound report")
    if candidate.app_commit != args.app_commit:
        raise ValueError("handoff app commit does not match the workflow candidate")
    repository_head = subprocess.run(
        ["git", "-C", str(args.app_repository), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if repository_head.returncode or repository_head.stdout.strip() != args.app_commit:
        raise ValueError("handoff verifier checkout is not at the exact candidate HEAD")
    if candidate.zeroth_version != args.zeroth_version:
        raise ValueError("handoff Zeroth version does not match the trusted certifier")
    if re.fullmatch(r"[0-9a-f]{40}", args.zeroth_commit) is None:
        raise ValueError("trusted certifier commit must be a full commit SHA")
    return candidate


def _validated_handoff_materials(
    args: argparse.Namespace,
    report: CertificationReport,
    candidate: CandidateIdentity,
) -> tuple[dict[str, str], str]:
    sbom = _destination(args.root, report.evidence.sbom.path)
    provenance = _destination(args.root, report.evidence.provenance.path)
    materials = _handoff_material_digests(args, candidate, sbom)
    validate_evidence_subject(
        "provenance",
        provenance,
        candidate,
        zeroth_commit=args.zeroth_commit,
        sbom_digest=file_digest(sbom),
        build_material_digests=materials,
    )
    validate_image_archive(args.image_archive, candidate)
    app_tree = validate_source_archive(
        args.source_archive, candidate, repository=args.app_repository
    )
    return materials, app_tree


def _validate_handoff(args: argparse.Namespace) -> int:
    workflow_evidence = validate_workflow_evidence(
        args.workflow_evidence,
        cleanup=args.cleanup,
        report=args.report,
        workflow_stages=args.workflow_stages,
    )
    report = validate_report(args.report, root=args.root)
    candidate = _validated_handoff_candidate(args, report)
    _materials, app_tree = _validated_handoff_materials(args, report, candidate)
    verdict = {
        "schema_version": 1,
        "app_commit": candidate.app_commit,
        "app_tree": app_tree,
        "zeroth_commit": args.zeroth_commit,
        "zeroth_version": candidate.zeroth_version,
        "image_reference": candidate.image_reference,
        "image_digest": candidate.image_digest,
        "source_digest": candidate.source_digest,
        "candidate_identity_digest": identity_digest(candidate),
        "cleanup_sha256": workflow_evidence["cleanup_sha256"],
        "report_sha256": file_digest(args.report),
        "workflow_evidence_sha256": file_digest(args.workflow_evidence),
        "workflow_stages_sha256": workflow_evidence["workflow_stages_sha256"],
        "image_archive_sha256": file_digest(args.image_archive),
        "provenance_path": report.evidence.provenance.path,
    }
    args.verdict.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"image_reference={candidate.image_reference}")
    print(f"image_digest={candidate.image_digest}")
    print(f"provenance_path={report.evidence.provenance.path}")
    print(f"verdict_sha256={file_digest(args.verdict)}")
    return 0


def _validate_command(args: argparse.Namespace) -> int:
    if args.command == "validate-declaration":
        declaration = load_declaration(args.declaration)
        root = args.root or args.declaration.parent
        declared_dockerfile = root.resolve() / declaration.dockerfile
        try:
            dockerfile = _destination(root, declaration.dockerfile)
        except ValueError as error:
            raise ValueError("declared dockerfile resolves outside the build context") from error
        if dockerfile != declared_dockerfile or not dockerfile.is_file():
            raise ValueError("declared dockerfile must be a regular file without symlinks")
        print(f"valid declaration: {args.declaration}")
        return 0
    report = validate_report(args.report, root=args.root)
    print(f"valid report: {args.report}; status={report.status}")
    return 0 if report.status == "passed" else 1


def _prepare_runtime_artifact(args: argparse.Namespace) -> int:
    if args.command == "verify-wheel-installation":
        verify_wheel_installation(
            args.wheel,
            args.site_packages,
            args.output,
            image_config=args.image_config,
            image_digest=args.image_digest,
        )
    else:
        prepare_runtime_context(
            args.source_root,
            args.certifier_wheel,
            args.requirements_lock,
            args.image_config,
            args.output,
        )
    return 0


def _finalize_attestation_command(args: argparse.Namespace) -> int:
    _validate_handoff(args)
    finalize_attestation(
        args.bundle,
        args.report,
        args.root,
        repository=args.repository,
        signer_repo=args.signer_repo,
        signer_workflow=args.signer_workflow,
        signer_digest=args.signer_digest,
    )
    write_workflow_evidence(
        args.workflow_evidence,
        cleanup=args.cleanup,
        report=args.report,
        workflow_stages=args.workflow_stages,
    )
    return _validate_handoff(args)


def _probe_readiness_command(args: argparse.Namespace) -> int:
    return _probe_readiness(args.url)


def _probe_runtime_command(args: argparse.Namespace) -> int:
    probe_runtime_extras(args.zeroth_version)
    return 0


def _probe_regulus_command(args: argparse.Namespace) -> int:
    probe_regulus(args.url, args.tenant, args.probe_id, args.mode)
    return 0


def _finalize_workflow_command(args: argparse.Namespace) -> int:
    return _finalize_workflow(args.root)


def _generate_semantic_command(args: argparse.Namespace) -> int:
    generate_semantic_manifest(
        args.root,
        load_declaration(args.declaration),
        args.output,
        database_backend=args.database_backend,
    )
    return 0


def _scaffold_command(args: argparse.Namespace) -> int:
    scaffold_checkout(
        args.root,
        app_name=args.app_name,
        module=args.module,
        zeroth_version=args.zeroth_version,
        zeroth_ref=args.zeroth_ref,
    )
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    handlers = {
        "validate-declaration": _validate_command,
        "validate-report": _validate_command,
        "run": _run,
        "prepare-evidence": _prepare_evidence,
        "verify-wheel-installation": _prepare_runtime_artifact,
        "prepare-runtime-context": _prepare_runtime_artifact,
        "validate-handoff": _validate_handoff,
        "finalize-attestation": _finalize_attestation_command,
        "probe-readiness": _probe_readiness_command,
        "probe-runtime-extras": _probe_runtime_command,
        "probe-regulus": _probe_regulus_command,
        "finalize-workflow": _finalize_workflow_command,
        "generate-semantic": _generate_semantic_command,
        "scaffold": _scaffold_command,
    }
    return handlers[args.command](args)


def main(argv: list[str] | None = None) -> int:
    """Validate declarations, execute certification, or validate retained reports."""
    args = _parser().parse_args(argv)
    try:
        return _dispatch(args)
    except (OSError, ValueError) as error:
        print(f"app certification error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
