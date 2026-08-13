"""Command-line entry point for app declaration and report validation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .models import SmokeSpec, load_declaration, validate_report, write_report
from .runner import CertificationRunner, HttpResult


class UrlHttpBoundary:
    """Execute declared smoke requests against two caller-supplied origins."""

    def __init__(self, packaged_url: str, ephemeral_url: str) -> None:
        self.urls = {
            "packaged-smoke": packaged_url.rstrip("/"),
            "ephemeral-smoke": ephemeral_url.rstrip("/"),
        }

    def __call__(self, check: str, smoke: SmokeSpec) -> HttpResult:
        body = json.dumps(smoke.request_json, sort_keys=True, separators=(",", ":")).encode()
        request = Request(
            self.urls[check] + smoke.path,
            data=body,
            method=smoke.method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310 - explicit caller URL
                return HttpResult(response.status, json.load(response))
        except HTTPError as error:
            return HttpResult(error.code, json.load(error))


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
    return parser


def _run(args: argparse.Namespace) -> int:
    declaration = load_declaration(args.declaration)
    http = UrlHttpBoundary(args.packaged_url, args.ephemeral_url)
    report = CertificationRunner(args.root, declaration, http=http).run(
        expected_commit=args.app_commit,
        image_digest=args.image_digest,
    )
    write_report(report, args.report)
    print(f"app certification: {report.status}; report={args.report}")
    return 0 if report.status == "passed" else 1


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
        report = validate_report(args.report)
        print(f"valid report: {args.report}; status={report.status}")
        return 0 if report.status == "passed" else 1
    except (OSError, ValueError) as error:
        print(f"app certification error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
