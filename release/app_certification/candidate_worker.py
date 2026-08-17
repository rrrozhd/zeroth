"""Supervise the only supported dynamic candidate contract: migration effects."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import checks
from .candidate_supervisor import probe_candidate
from .migration_supervisor import inspect_migration
from .models import AppDeclaration

CANDIDATE_CHECKS = frozenset({"migrations"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _trusted_evidence(
    name: str, payload: Any, declaration: AppDeclaration, root: Path
) -> dict[str, Any]:
    expected_keys = {"check", "evidence", "schema_version", "target_sources"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ValueError("candidate evidence has no trusted finalization payload")
    if payload.get("check") != name or payload.get("schema_version") != 1:
        raise ValueError("candidate evidence does not match the requested check")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("candidate migration evidence is malformed")
    if payload["target_sources"] != checks.target_source_digests(name, root, declaration):
        raise ValueError("candidate evidence does not match the declared migration source")
    return evidence


def finalize_candidate_evidence(
    name: str, payload: Any, declaration: AppDeclaration, root: Path
) -> None:
    """Validate trusted postcondition evidence, never candidate-authored result data."""
    if name != "migrations":
        raise ValueError(f"dynamic candidate check {name!r} is not supported")
    evidence = _trusted_evidence(name, payload, declaration, root)
    if set(evidence) != {"backend", "object_count", "runner", "schema_sha256"}:
        raise ValueError("candidate migration evidence is malformed")
    if evidence.get("backend") not in {"sqlite", "postgres"}:
        raise ValueError("candidate migration backend evidence is unsupported")
    if evidence.get("backend") != checks.validated_database_backend(root, declaration):
        raise ValueError("candidate migration backend does not match the semantic declaration")
    if evidence.get("runner") != declaration.targets.migration_runner:
        raise ValueError("candidate migration runner does not match the declaration")
    if not isinstance(evidence.get("object_count"), int) or evidence["object_count"] <= 0:
        raise ValueError("candidate migration schema is empty")
    digest = evidence.get("schema_sha256")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ValueError("candidate migration schema digest is invalid")


def _supervised_payload(
    evidence: dict[str, Any], root: Path, declaration: AppDeclaration
) -> dict[str, Any]:
    return {
        "check": "migrations",
        "evidence": evidence,
        "schema_version": 1,
        "target_sources": checks.target_source_digests("migrations", root, declaration),
    }


def _supervise_migration(
    root: Path,
    declaration: AppDeclaration,
    candidate_venv: Path,
    untrusted_user: str | None,
) -> dict[str, Any]:
    backend = checks.validated_database_backend(root, declaration)
    runtime_backend = os.environ.get("ZEROTH_DATABASE__BACKEND", "sqlite")
    if runtime_backend != backend:
        raise ValueError(
            f"declared database backend {backend} does not match runtime backend "
            f"{runtime_backend}"
        )
    postgres_dsn = os.environ.get("ZEROTH_DATABASE__POSTGRES_DSN")

    def run_candidate(reference: str, database_url: str) -> None:
        probe_candidate(
            "run-migration",
            root,
            candidate_venv,
            reference=reference,
            database_url=database_url,
            untrusted_user=untrusted_user,
        )

    return _supervised_payload(
        inspect_migration(
            declaration,
            run_candidate,
            backend=backend,
            postgres_dsn=postgres_dsn,
        ),
        root,
        declaration,
    )


def _supervise_candidate(
    root: Path,
    declaration: AppDeclaration,
    candidate_venv: Path,
    untrusted_user: str | None,
) -> int:
    try:
        payload = _supervise_migration(root, declaration, candidate_venv, untrusted_user)
        finalize_candidate_evidence("migrations", payload, declaration, root)
    except Exception as error:  # noqa: BLE001 - candidate effects are untrusted
        print(f"migrations: trusted finalization failed in supervisor: {error}", file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m release.app_certification.candidate_worker")
    parser.add_argument("name", choices=("migrations",))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--declaration-json", required=True)
    parser.add_argument("--candidate-venv", type=Path, required=True)
    parser.add_argument("--untrusted-user")
    args = parser.parse_args(argv)
    return _supervise_candidate(
        args.root.resolve(),
        AppDeclaration.model_validate_json(args.declaration_json),
        args.candidate_venv,
        args.untrusted_user,
    )


if __name__ == "__main__":
    raise SystemExit(main())
