"""Command-line entry point for app declaration and report validation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .evidence import (
    bind_sbom,
    finalize_attestation,
    validate_image_archive,
    write_provenance,
)
from .models import SmokeSpec, load_declaration, validate_report, write_report
from .runner import CertificationRunner, HttpResult, measure_candidate_identity
from .scaffold import scaffold_checkout


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
        body = json.dumps(smoke.request_json, sort_keys=True, separators=(",", ":")).encode()
        request = Request(
            self.urls[check] + smoke.path,
            data=body,
            method=smoke.method,
            headers=self.headers,
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - explicit caller URL
                return HttpResult(response.status, json.load(response))
        except HTTPError as error:
            return HttpResult(error.code, json.load(error))


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
    run.add_argument("--packaged-url", required=True)
    run.add_argument("--ephemeral-url", required=True)
    run.add_argument("--report", type=Path, required=True)
    report = commands.add_parser("validate-report")
    report.add_argument("--report", type=Path, required=True)
    report.add_argument("--root", type=Path)
    evidence = commands.add_parser("prepare-evidence")
    evidence.add_argument("--declaration", type=Path, required=True)
    evidence.add_argument("--root", type=Path, required=True)
    evidence.add_argument("--app-commit", required=True)
    evidence.add_argument("--image-digest", required=True)
    evidence.add_argument("--raw-sbom", type=Path, required=True)
    handoff = commands.add_parser("validate-handoff")
    handoff.add_argument("--report", type=Path, required=True)
    handoff.add_argument("--root", type=Path, required=True)
    handoff.add_argument("--image-archive", type=Path, required=True)
    handoff.add_argument("--app-commit", required=True)
    handoff.add_argument("--zeroth-version", required=True)
    attestation = commands.add_parser("finalize-attestation")
    attestation.add_argument("--bundle", type=Path, required=True)
    attestation.add_argument("--report", type=Path, required=True)
    attestation.add_argument("--root", type=Path, required=True)
    probe = commands.add_parser("probe-readiness")
    probe.add_argument("--url", required=True)
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
        http=http,
        declaration_path=args.declaration,
    ).run(
        expected_commit=args.app_commit,
        image_digest=args.image_digest,
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
    validate_image_archive(args.image_archive, candidate)
    print(f"image_reference={candidate.image_reference}")
    print(f"image_digest={candidate.image_digest}")
    print(f"provenance_path={report.evidence.provenance.path}")
    return 0


def _probe_readiness(url: str) -> int:
    with urlopen(url, timeout=5) as response:  # noqa: S310 - fixed workflow URL
        payload = json.load(response)
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
            return 0
        if args.command == "probe-readiness":
            return _probe_readiness(args.url)
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
