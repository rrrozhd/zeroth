#!/usr/bin/env python3
"""CLI for candidate identity, gate records, fail-closed validation, verdict.

Invoked by file path from CI (``python release/gates/cli.py ...``), the same
way ``release/langgraph/harness.py`` is, so no packaging or install step sits
between a workflow step and the gate it has to satisfy.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # invoked as a path, not as a module
    # Deliberately NOT ``sys.path.insert(0, release/)``. ``release/langgraph``
    # would then become a PEP 420 namespace portion named ``langgraph`` that
    # sorts ahead of the installed LangGraph library -- both are namespace
    # packages, so the portions merge and ours wins the ordering. Registering
    # this package by explicit spec keeps sys.path untouched.
    import importlib.util

    _here = Path(__file__).resolve().parent
    _spec = importlib.util.spec_from_file_location(
        "gates", _here / "__init__.py", submodule_search_locations=[str(_here)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["gates"] = _module
    _spec.loader.exec_module(_module)

from gates.identity import (  # noqa: E402
    candidate_identity,
    file_digest,
    identity_digest,
)
from gates.manifest import (  # noqa: E402
    DEFAULT_MANIFEST,
    TRIGGERS,
    load_manifest,
    select_gates,
)
from gates.validate import PASSED, releasable, validate  # noqa: E402
from gates.verdict import render  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
RECORD_SCHEMA_VERSION = 1


def _pairs(values: list[str] | None, label: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw in values or []:
        name, separator, value = raw.partition("=")
        if not separator or not name or not value:
            raise SystemExit(f"--{label} expects name=value, got {raw!r}")
        parsed[name] = value
    return parsed


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    identity = commands.add_parser("identity", help="measure the candidate under release")
    identity.add_argument("--root", type=Path, default=ROOT)
    identity.add_argument("--commit")
    identity.add_argument("--artifact", action="append", metavar="NAME=PATH")
    identity.add_argument("--image", action="append", metavar="REFERENCE=DIGEST")
    identity.add_argument("--configuration", type=Path)
    identity.add_argument("--compatibility", type=Path)
    identity.add_argument("--output", type=Path, required=True)

    record = commands.add_parser("record", help="emit one gate's evidence record")
    record.add_argument("--gate", required=True)
    record.add_argument("--identity", type=Path, required=True)
    record.add_argument("--result", action="append", metavar="NAME=STATUS")
    record.add_argument("--kind", action="append", metavar="NAME=PATH")
    record.add_argument("--status", help="override the status derived from the results")
    record.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    record.add_argument("--output", type=Path)

    check = commands.add_parser("validate", help="fail closed unless every gate holds")
    check.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    check.add_argument("--identity", type=Path, required=True)
    check.add_argument("--evidence-root", type=Path, default=ROOT)
    check.add_argument("--phase", choices=("candidate", "final"), default="final")
    check.add_argument("--trigger", choices=TRIGGERS)

    report = commands.add_parser("verdict", help="render the human-readable verdict")
    report.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    report.add_argument("--identity", type=Path, required=True)
    report.add_argument("--evidence-root", type=Path, default=ROOT)
    report.add_argument("--phase", choices=("candidate", "final"), default="final")
    report.add_argument("--trigger", choices=TRIGGERS)
    report.add_argument("--output", type=Path)

    seal = commands.add_parser(
        "seal", help="write the evidence manifest that attestation signs over"
    )
    seal.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    seal.add_argument("--identity", type=Path, required=True)
    seal.add_argument("--evidence-root", type=Path, default=ROOT)
    seal.add_argument("--phase", choices=("candidate", "final"), default="final")
    seal.add_argument("--output", type=Path, required=True)

    fingerprint = commands.add_parser(
        "digest", help="print the candidate identity digest a signoff must name"
    )
    fingerprint.add_argument("--identity", type=Path, required=True)
    return parser


def _gate(manifest: dict[str, Any], identifier: str) -> dict[str, Any]:
    for gate in manifest["gates"]:
        if gate["id"] == identifier:
            return gate
    raise SystemExit(f"unknown gate {identifier!r}")


def _identity(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"candidate identity unreadable: {error}") from error


def _do_identity(args: argparse.Namespace) -> int:
    artifacts = {name: Path(value) for name, value in _pairs(args.artifact, "artifact").items()}
    identity = candidate_identity(
        args.root,
        commit=args.commit,
        artifacts=artifacts,
        image=_pairs(args.image, "image"),
        configuration=args.configuration,
        compatibility=args.compatibility,
    )
    _write_json(args.output, identity)
    print(f"candidate identity {identity_digest(identity)}")
    return 0


def _do_record(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    gate = _gate(manifest, args.gate)
    candidate = _identity(args.identity)
    results = _pairs(args.result, "result")
    # The record binds only what its gate declares, so a record that omits a
    # required facet is detectable rather than silently permissive.
    bound = {facet: candidate[facet] for facet in gate["binds"] if facet in candidate}
    status = args.status or (
        PASSED if results and all(value == PASSED for value in results.values()) else "failed"
    )
    record = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "gate": gate["id"],
        "status": status,
        "identity": bound,
        "results": results,
        "kinds": _pairs(args.kind, "kind"),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    _write_json(args.output or (ROOT / gate["record"]), record)
    print(f"{gate['id']}: {status}")
    return 0


def _do_validate(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    results = validate(
        manifest,
        _identity(args.identity),
        args.evidence_root,
        phase=args.phase,
        trigger=args.trigger,
    )
    for result in results:
        print(f"{result.gate}: {result.status} — {result.reason}")
    if not releasable(results):
        blocking = [result.gate for result in results if result.blocking]
        print(f"::error::release blocked by: {', '.join(blocking)}", file=sys.stderr)
        return 1
    print(f"all {len(results)} {args.phase}-phase gates hold")
    return 0


def _do_verdict(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    candidate = _identity(args.identity)
    results = validate(
        manifest, candidate, args.evidence_root, phase=args.phase, trigger=args.trigger
    )
    document = render(manifest, candidate, results, phase=args.phase, trigger=args.trigger)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(document, encoding="utf-8")
    else:
        print(document, end="")
    return 0 if releasable(results) else 1


def _do_seal(args: argparse.Namespace) -> int:
    """Bind the candidate and every record into one digest-carrying document.

    This is what build attestation signs over. Attesting the image alone leaves
    the evidence itself unsigned, so anyone able to write a record could forge
    a passing one by copying the public candidate identity. Sealing puts every
    record's digest under the same signature as the artifact.
    """
    manifest = load_manifest(args.manifest)
    candidate = _identity(args.identity)
    sealed: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "phase": args.phase,
        "candidate": candidate,
        "candidate_digest": identity_digest(candidate),
        "records": {},
    }
    for gate in select_gates(manifest, phase=args.phase):
        path = args.evidence_root / gate["record"]
        sealed["records"][gate["id"]] = (
            file_digest(path) if path.is_file() else "absent"
        )
    _write_json(args.output, sealed)
    print(sealed["candidate_digest"])
    return 0


def _do_digest(args: argparse.Namespace) -> int:
    print(identity_digest(_identity(args.identity)))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return its exit status."""
    args = _parser().parse_args(argv)
    handlers = {
        "identity": _do_identity,
        "record": _do_record,
        "validate": _do_validate,
        "verdict": _do_verdict,
        "seal": _do_seal,
        "digest": _do_digest,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
