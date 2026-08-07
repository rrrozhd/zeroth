#!/usr/bin/env python3
"""CLI for reproducible LangGraph release evidence and runtime probes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from langgraph_benchmark import benchmark
from release_evidence import validate_manifest
from runtime_smoke import (
    gateway_smoke,
    resolved_image_evidence,
    serve_mock_upstream,
    smoke,
)

ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    bench = commands.add_parser("benchmark")
    bench.add_argument("--samples", type=int, default=20)
    bench.add_argument("--inject-regression", action="store_true")
    bench.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("validate")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--phase", choices=("source", "final"), default="source")
    check.add_argument("--evidence-root", type=Path, default=ROOT)
    probe = commands.add_parser("smoke")
    probe.add_argument("--url", default="http://127.0.0.1:8000/health/ready")
    probe.add_argument("--require-gateway", action="store_true")
    gateway_probe = commands.add_parser("gateway-smoke")
    gateway_probe.add_argument(
        "--url", default="http://127.0.0.1:8000/assistants/release-smoke"
    )
    gateway_probe.add_argument("--api-key", required=True)
    fixture = commands.add_parser("mock-upstream")
    fixture.add_argument("--host", default="0.0.0.0")
    fixture.add_argument("--port", type=int, default=8123)
    images = commands.add_parser("image-evidence")
    images.add_argument("--image", action="append", required=True)
    images.add_argument("--output", type=Path, required=True)
    return parser


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _benchmark(args: argparse.Namespace) -> int:
    report = benchmark(args.samples, inject_regression=args.inject_regression)
    _write_json(args.output, report)
    if not report["passed"]:
        print("threshold failed", file=sys.stderr)
        return 1
    return 0


def _validate(args: argparse.Namespace) -> int:
    errors = validate_manifest(
        args.manifest, phase=args.phase, evidence_root=args.evidence_root
    )
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("release evidence complete")
    return 0


def _smoke(args: argparse.Namespace, *, gateway: bool) -> int:
    try:
        if gateway:
            gateway_smoke(args.url, args.api_key)
        else:
            smoke(args.url, require_gateway=args.require_gateway)
    except Exception as error:  # noqa: BLE001 - CLI returns a stable failure
        label = "gateway smoke" if gateway else "smoke"
        print(f"{label} failed: {error}", file=sys.stderr)
        return 1
    print("gateway smoke passed" if gateway else "readiness smoke passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "benchmark":
        return _benchmark(args)
    if args.command == "validate":
        return _validate(args)
    if args.command == "smoke":
        return _smoke(args, gateway=False)
    if args.command == "gateway-smoke":
        return _smoke(args, gateway=True)
    if args.command == "image-evidence":
        _write_json(args.output, resolved_image_evidence(args.image))
        return 0
    serve_mock_upstream(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
